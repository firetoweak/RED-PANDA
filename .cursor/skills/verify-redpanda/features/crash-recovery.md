# 异常恢复

进程被杀、沙箱/FUSE 死掉、断网、模型 API 出错之后，会话能否正确续上：journal 状态、执行一半的工具、待授权、挂载清理、重连后的界面、重试不重复。来源见各子功能。

源码事实（判据以此为准）：
- **进程结构**：Web 进程内的 `HostSupervisor`（`redpanda/assistant/host/supervisor.py`）为每个会话起 Worker 子进程；Worker 失败包装为 `WorkerFailed`，经 `report_worker_failures`（`redpanda/channels/web/app.py`）发 SSE `session_failed`，文案 `Session 进程失败：...`。
- **执行一半的工具**：journal 里已记 attempt 但无结果的命令处于 `CommandPhase.UNKNOWN`（`redpanda/runtime/state.py`）。给模型的投影是 `NO_RESULT_YET`：「该调用已开始执行但还没有结果……可能仍在执行，也可能已中断；重试前先确认副作用。」（`redpanda/assistant/context/projection.py`）；Web 显示为 `执行中断，结果未知`（`redpanda/assistant/conversations.py` 的 `UNKNOWN_TOOL_ERROR`）。**不会自动重试**。子 Agent 同理（`record_interrupted_return`）。
- **待授权**：未授权的命令投影为 `AWAITING_AUTHORIZATION`，「尚未执行」。journal 对同一命令重复授权/拒绝会抛 `command already authorized/rejected`（`redpanda/runtime/journal/api.py`）。
- **消息去重**：外部输入按 `(delivery_source, delivery_id)` 唯一（`journal/sqlite.py` 的 `events_delivery_identity` 唯一索引）；同一 `delivery_id` 同内容重发返回已有事件，不同内容抛 `DeliveryConflictError`（它是 `ValueError` 的子类）。
- **模型错误**（`thinllm/chat_completions.py`、`redpanda/assistant/failures.py`）：408/425/429/500/502/503/504 与 `httpx.TransportError` → `LLMTransientError`，**仅在流尚未开始时**按 0.5/1/2 秒重试 3 次；流一旦开始（收到任何一行）就不再重试。401/403 → `模型认证失败：API 密钥无效……`；`context_length_exceeded` → `模型输入超出上下文限制`；流没有 `[DONE]` → `InvalidLLMResponse`（`模型响应不符合约定`）；空回复 → `模型这一拍没有给出可用回复或工具调用。下一条消息会从同一触发重试。`。读超时 60 秒、连接超时 10 秒。
- **失败可见与再试**：已识别的模型请求失败只停该会话，当前失败原因记录在会话上（`session.last_error`），刷新后仍可查询；`runnable` 只表示仍有待处理输入，不代表模型请求正在执行。点 `重试`沿用原待处理输入继续，不新增用户消息、不重复已完成的工具。
- **FUSE 挂载**：`native/sandbox/src/main.rs` 的 `clear_stale_mount` 在下次挂载前遇到 `ENOTCONN` 时执行 `fusermount3 -u -z` 清理残留挂载。崩溃契约测试只有 Windows（`native/vfs/crates/vfs-mount/tests/windows_execution_crash.rs`），**Linux 没有对应的崩溃测试**。
- **定时任务**：`redpanda/automation/recovery.py` 处理重启后 UNKNOWN 的定时命令。

## Sub-features

- `kill-web`：`kill -9` Web 进程（连同 Worker）后同 home 重启。
- `kill-worker`：只 `kill -9` 某会话的 Worker 子进程。
- `kill-sandbox`：工具执行中杀掉 `redpanda-sandbox`/FUSE 进程。
- `network-loss`：模型请求期间断网。
- `model-errors`：超时、429、5xx、流被截断、无效 key。
- `pending-auth-survives`：待授权跨重启保留。
- `resend-idempotent`：同一 `delivery_id` 重发不重复。
- `ui-reconnect`：重连后界面与 journal 一致。

## How to get to it (user POV)

- 用户视角就是「程序崩了 / 网断了 / 模型报错了，重新打开页面继续用」。驱动方法是在 shell 里制造故障，然后在浏览器里看结果。
- 模型错误建议用本地假服务：在 `config.json`/`connections.json` 里把一个供应商的 base URL 指到 `http://127.0.0.1:<port>`，用一个小 HTTP 服务按需返回 429/500/截断流/401。

## Driving it with the browser + shell

记录故障前的基线：`sqlite3 <会话 journal.sqlite> 'select count(*) from events'`、工作区文件哈希、`mount | grep -i fuse`、`pgrep -af redpanda`。

- **kill -9 Web（工具执行中）**：让模型 `execute_command` 跑 `sleep 30 && echo DONE >> out.txt`，运行中 `kill -9 $(cat web.pid)`，同 home 重启。
  - (a) journal 中该命令为 UNKNOWN（`GET /api/sessions/<id>` 中该步状态 `unknown`，界面 `执行中断，结果未知`）；事件计数不少于基线 + 已写事件（没有丢已提交事件）；`out.txt` 是否存在都可以，但**之后不得再出现第二行 `DONE`**。
  - (b) 重启后页面从 `正在连接…` 变为 `已连接`；会话侧栏不应永远停在 `运行中`。
  - (c) 其他会话 journal 不变；工作区里与该命令无关的文件不变。
  - (d) 发 `继续` 或点 `重试` → 模型收到 `NO_RESULT_YET` 提示，应先检查 `out.txt` 再决定；若系统自动重跑了该命令（`out.txt` 出现两行），判失败。
  - 残留：`pgrep -af 'sleep 30'` 应为空或在 30 秒内结束；`mount | grep -i fuse` 无残留挂载，或下一次文件工具调用前被 `clear_stale_mount` 清掉（再次跑 `read_file` 成功即证）。
- **kill -9 Worker**：只杀 Worker 子进程。(b) SSE 出现 `session_failed`，界面显示 `Session 进程失败：...`。(a) 再发消息时 Worker 重新拉起，历史完整。(c) Web 进程与其他会话不受影响。
- **kill 沙箱/FUSE 进程**：`apply_patch` 或 `execute_command` 期间 `pkill -9 redpanda-sandbox`。(a) 该工具以失败或 UNKNOWN 结束，不得报告成功；工作区文件要么是旧内容要么是新内容，不得是半截。(c) 用户工作区目录本身仍可访问（`ls $SCRATCH_WORKSPACE` 不报 `Transport endpoint is not connected`）。(d) 立即再发一次文件操作 → 成功（残留挂载被清理）。若挂载点残留且后续工具一直失败，记为问题。
- **断网**：模型请求中 `iptables` 或关闭网卡阻断出站（或把 base URL 指向不监听的端口）。(b) 约 3.5 秒内（0.5+1+2 秒重试）出现 `模型服务暂时不可用：... transport failed`。(a) journal 有失败事实，没有半条助手消息。(d) 恢复网络后点 `重试` → 同一触发重跑，不重复用户消息（`GET` 里该用户消息只有一条）。
- **429 / 5xx（流开始前）**：假服务前两次返回 429，第三次正常 → (a) 回合成功，journal 只有一条助手回复。四次都 429 → `模型服务暂时不可用：<provider> 429: ...`。
- **流中断**：假服务先发几个 `data:` 块再断开、不发 `[DONE]` → (b) `模型响应不符合约定：model stream ended without the [DONE] marker` 或传输错误，**不重试**。(a) 已流出的预览文字不作为最终回复写入 journal（界面 `preview.aborted`）。
- **超时**：假服务 70 秒不回 → 60 秒读超时后报临时错误；界面不得永久转圈。
- **无效 key**：(b) `模型认证失败：API 密钥无效，或当前密钥无权访问配置的模型。请检查个人目录 connections.json 中当前模型供应商的 API 密钥。` (c) 不重试，响应与日志中不出现 key 明文。(d) 刷新页面后仍显示该失败原因；恢复配置后点 `重试` → 同一触发重跑，不重复用户消息。
- **待授权跨重启**：出现 `待授权` 后 `kill -9` 重启。(a) 命令仍在等待授权（投影 `AWAITING_AUTHORIZATION`），文件未改动。(b) 界面仍显示 `允许`/`拒绝`。(d) 重启后点 `允许` → 只执行一次；对同一 `command_id` 再 POST `authorize` → 预期 4xx；若返回 500，记为疑似问题（`command already authorized` 是 `ValueError`，`app.py` 未注册 `ValueError` 处理器）。
- **重发去重**：用浏览器开发者工具把一次 `POST .../inputs` 原样重放（同一 `delivery_id`）。(a) `GET` 中消息只有一条，journal 事件数不变。(d) 同一 `delivery_id` 但改了 `text` 重放 → 应 409/4xx；若 500 记为疑似问题（`DeliveryConflictError` 未被处理）。
- **重连界面**：每个故障后刷新页面，(a) 转录、步骤状态、`当前计划`、子 Agent 卡片与 `GET /api/sessions/<id>` 一致。

## Gotchas

- **疑似问题：Linux 崩溃路径没有测试。** 只有 Windows 有崩溃契约测试，Linux FUSE 的崩溃后清理只靠 `clear_stale_mount`，必须 live 验。
- **疑似问题：流开始的判定太早。** `stream.started` 在收到任何一行（包括空行/keep-alive）时就置真，之后的 5xx/断流不再重试；`Retry-After` 头没有被读取。
- 已知入口错误已分类：失效连接 409、空消息 422、授权目标不存在 404、重复授权/拒绝 409；内部契约违规与未知异常仍原样暴露，不做全局兜底。
- `kill -9` Web 会连带 Worker 子进程失去父进程；用 `pgrep` 确认没有孤儿 Worker 继续写 journal。
- 这些测试会故意破坏环境，只在临时 `REDPANDA_HOME` 与临时工作区里做。

## Live status
- 部分（2026-10-12，修复后）：模型失败可见性与再试已由隔离 API 注入认证失败验证（16/16，见 `.tools/verification/fixapi-20261012/`）；已知入口错误分类（自动化通过）；kill -9 后工具不重复执行（通过）
- 未验（待真实 UI / 环境）：刷新后错误仍在页面上的观感、真实供应商 429 与 `Retry-After`、Linux FUSE 残留清理
- 更严格成功标准：模型调用失败后刷新页面仍能看到失败原因，再试沿用原输入且不重复已完成工具——API 层已满足，UI 层待人工复跑
