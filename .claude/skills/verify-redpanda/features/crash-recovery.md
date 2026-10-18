# 异常恢复

进程被杀、断网、模型 API 出错之后，会话能否正确续上：journal 状态、执行一半的工具、待授权、重连后的界面、再试不重复。

源码事实（判据以此为准，2026-10-18 核对）：
- **进程结构**：Web 进程内的 `HostSupervisor`（`redpanda/assistant/host/supervisor.py`）为每个会话用 `multiprocessing` spawn 起 Worker 子进程（命令行含 `spawn_main`）；Worker 在父进程退出后自行退出（`worker.py` 的 `parent_process().join()`）。Worker 失败经 `report_worker_failures`（`redpanda/channels/web/app.py`）发 SSE `session_failed`，文案 `Session 进程失败：...`，同一文案也记为该会话的 `last_error`。
- **重启自动停住**：Web 启动时 `pause_unfinished_sessions` 把仍会自己往下走（`should_wake`）的顶层会话置为 paused；子会话不改，由父恢复时带起。所以重启后选中会话不会自动续跑，要点 `继续`（或发新消息）。
- **执行一半的工具**：journal 已记 attempt 但无结果的命令处于 `CommandPhase.UNKNOWN`（`redpanda/runtime/state.py`）。新 Worker 接管或 Web 启动时，`SqliteJournal.prepare_recovery` 为每个未完成 attempt 追加 `command.attempt.orphaned`（不是 Outcome，状态仍 unknown）；之后新消息不再被它挡住，`再试`/`继续` 会让模型看到 `NO_RESULT_YET` 并续步。给模型的投影 `NO_RESULT_YET`（`redpanda/assistant/context/projection.py`）：error「该调用已开始执行但没有结果，原因未知，可能仍在执行或已部分执行（工具=<name>）。」，hint「先检查相关文件、外部状态和仍在运行的操作，再决定等待、补做或重试。」。Web 中该调用状态 `unknown`、徽标 `中断`，错误文字 `执行已开始，但未取得最终结果，原因未知，可能已部分执行`（`redpanda/assistant/conversations.py` 的 `UNKNOWN_TOOL_ERROR`）。**不会自动重试**。子 Agent 同理（`record_interrupted_return`）。
- **待授权**：未授权的命令投影为 `AWAITING_AUTHORIZATION`，「尚未执行」。对已处理的命令再授权/拒绝 → `AuthorizationConflict`「该操作已处理或当前不可授权：<command_id>」→ HTTP 409。
- **消息去重**：外部输入按 `(delivery_source, delivery_id)` 唯一（`journal/sqlite.py`）；同一 `delivery_id` 同内容重发返回已有事件，不同内容抛 `DeliveryConflictError`。
- **模型错误**（`thinllm/chat_completions.py`、`redpanda/assistant/failures.py`）：超时为 `connect=10, read=60, write=30, pool=10` 秒。408/425/429/500/502/503/504 与 `httpx.TransportError`（含 `ReadTimeout`、`ConnectTimeout`、连接被拒）→ `LLMTransientError`，**仅在流尚未开始时**按 0.5/1/2 秒重试 3 次（共 4 次尝试）；收到任何一行后就不再重试。401/403 → `模型认证失败：API 密钥无效……`；`context_length_exceeded` → `模型输入超出上下文限制`；流没有 `[DONE]` → `模型响应不符合约定`；空回复 → `模型这一拍没有给出可用回复或工具调用。下一条消息会从同一触发重试。`。
- **失败可见与再试**：已识别的模型失败只停该会话，SSE `session_failed` 与 `last_error` 文案均为 `运行失败：<原因>`（`redpanda/assistant/runner.py`）；界面横幅 `执行已停止。运行失败：…`，按钮 `查看详情`、`再试`。`last_error` 只在 Web 进程内存里，刷新页面仍在，重启 Web 后消失。`再试` 沿用原待处理输入，不新增用户消息、不重复已完成工具。
- **定时任务**：`redpanda/automation/recovery.py` 只补全重启前处于 UNKNOWN 的 `schedule_once`/`cancel_schedule` 调用；定时本身的持久与触发见 scheduling。

## Sub-features

- `kill-web`：`kill -9` Web 进程后同 home 重启。
- `kill-worker`：只杀某会话的 Worker 子进程。
- `network-loss`：模型请求期间断网（连接被拒 / 丢包）。
- `model-errors`：超时、429、5xx、流被截断、无效 key。
- `pending-auth-survives`：待授权跨重启保留。
- `resend-idempotent`：同一 `delivery_id` 重发不重复。
- `ui-reconnect`：重连后界面与 journal 一致。

## How to get to it (user POV)

- 用户视角就是「程序崩了 / 网断了 / 模型报错了，重新打开页面继续用」。驱动方法是在 shell 里制造故障，然后在浏览器里看结果。
- 模型错误用本地假服务：把一个供应商的 base URL 指到 `http://127.0.0.1:<port>`，用小 HTTP 服务按需返回 429/500/截断流/401/不回应。
- 只杀本实例的进程（`$RUN_DIR/web.pid` 来自 `../SKILL.md` 的启动步骤），不要按名字批量杀：
  - Linux：`web=$(cat "$RUN_DIR/web.pid")`；Worker 列表 `pgrep -a -P "$web" -f spawn_main`；杀 Web `kill -9 "$web"`；杀某个 Worker `kill -9 <上面列出的 pid>`。
  - Windows（PowerShell）：
    ```powershell
    $web = [int](Get-Content "$env:RUN_DIR\web.pid")
    $tree = @($web) + @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$web" | ForEach-Object ProcessId)
    Get-CimInstance Win32_Process | Where-Object { $tree -contains $_.ParentProcessId -and $_.CommandLine -like '*spawn_main*' } | Select-Object ProcessId, ParentProcessId, CommandLine
    ```
    （第二层是为 venv 的 `python.exe` 转发进程留的。）杀 Web `Stop-Process -Id $web -Force`；杀某个 Worker `Stop-Process -Id <上面列出的 ProcessId> -Force`。
  - 要杀特定会话的 Worker 时，保证当时只有这一个会话有 Worker（只开这一个会话、先等其他 Worker 空闲退出），列表里只剩一条再动手。

## Driving it with the browser + shell

记录故障前的基线：`sqlite3 <会话 journal.sqlite> 'select count(*) from events'`、工作区文件哈希、上面的 Worker 列表。

- **kill -9 Web（工具执行中）**：让模型 `execute_command` 跑 `sleep 30 && echo DONE >> out.txt`，运行中杀 Web，同 home 重启。
  - (a) 该命令状态 `unknown`（`GET /api/sessions/<id>`），界面徽标 `中断`、文字 `执行已开始，但未取得最终结果，原因未知，可能已部分执行`；`session.paused` 为 true；事件数不少于基线 + 已提交事件；`out.txt` 是否存在都可以，但**之后不得再出现第二行 `DONE`**。
  - (b) 重启后侧栏底部从 `正在连接…` 变为 `已连接`；会话不会自动续跑，输入框出现 `继续`；侧栏不应永远停在运行状态。
  - (c) 其他会话 journal 不变；工作区里与该命令无关的文件不变；重启前记下的 Worker pid 都已不在（无孤儿继续写 journal）。
  - (d) 点 `继续` → 模型读到 `NO_RESULT_YET`，应先检查 `out.txt` 再决定；若系统自动重跑了该命令（`out.txt` 出现两行），判失败。残留：`sleep 30` 在 30 秒内结束。
- **kill -9 Worker**：只杀 Worker。(b) SSE `session_failed`，界面横幅 `执行已停止。Session 进程失败：...`，按钮 `再试`。(a) 点 `再试` 或再发消息时 Worker 重新拉起，历史完整。(c) Web 进程与其他会话不受影响。(d) Web 已断开时点 `再试` → `再试失败，请确认后端连接后重试。`。
- **断网**：
  - 连接被拒（base URL 指向不监听的端口）：(b) 约 3.5 秒（0.5+1+2 秒重试等待）后横幅 `执行已停止。运行失败：模型服务暂时不可用：... transport failed`。
  - 丢包（`iptables -j DROP` 等，连接不上也不回绝）：(b) 约 4×10 + 3.5 ≈ 43.5 秒后出现同类失败。
  - (a) journal 有失败事实，没有半条助手消息。(c) 用户消息只有一条。(d) 恢复网络后点 `再试` → 同一触发重跑，`GET` 里该用户消息仍只有一条。
- **429 / 5xx（流开始前）**：假服务前两次 429、第三次正常 → (a) 回合成功，只有一条助手回复。四次都 429 → (b) `运行失败：模型服务暂时不可用：<provider> 429: ...`。
- **流中断**：假服务先发几个 `data:` 块再断开、不发 `[DONE]` → (b) `模型响应不符合约定：model stream ended without the [DONE] marker` 或传输错误，**不重试**。(a) 已流出的预览文字不作为最终回复写入 journal（SSE `preview.aborted`）。
- **超时**：
  - 假服务接受请求但一行都不回：读超时算临时错误且流未开始，会重试 → (b) 约 4×60 + 3.5 ≈ 243.5 秒后才出现失败横幅；期间界面保持运行中，不得永久转圈。
  - 假服务先回一行再停住：流已开始 → 最后一行后约 60 秒失败，不重试。
- **无效 key**：(b) `运行失败：模型认证失败：API 密钥无效，或当前密钥无权访问配置的模型。请检查个人目录 connections.json 中当前模型供应商的 API 密钥。` (c) 不重试，响应与日志中不出现 key 明文。(d) 刷新页面后横幅仍在；恢复配置后点 `再试` → 同一触发重跑，不重复用户消息。
- **待授权跨重启**：出现 `待授权` 后杀 Web 重启。(a) 命令仍为 `awaiting_authorization`，文件未改动。(b) 界面仍显示授权按钮。(c) 授权前不执行。(d) 重启后点允许 → 只执行一次；对同一 `command_id` 再 POST `authorize` → 409，`detail` 为 `该操作已处理或当前不可授权：<command_id>`。
- **重发去重**：用开发者工具把一次 `POST .../inputs` 原样重放（同一 `delivery_id`）。(a) `GET` 中消息只有一条，journal 事件数不变。(b) 重放返回 2xx。(c) 会话不新开回合。(d) 同一 `delivery_id` 但改了 `text` 重放 → 预期 409/4xx，会话与 Worker 不受影响（当前实现见 Gotchas 的产品缺口）。
- **重连界面**：每个故障后刷新页面，(a) 转录、步骤状态、`当前计划`、子 Agent 卡片与 `GET /api/sessions/<id>` 一致。

## Gotchas

- 已知产品缺口（2026-10-18 源码核对，待 live 确认）：同一 `delivery_id` 改了内容重放时，`DeliveryConflictError` 在 Worker 里抛出；`redpanda/assistant/host/ipc.py` 的 `_handle` 只把授权类错误转成可回传的错误，其他异常让 Worker 失败，因此很可能表现为 500 + `session_failed`（`Session 进程失败：...`），而不是 409。判据保持 409/4xx 且会话不受影响。
- 疑似问题：流开始判定太早。收到任何一行（包括空行/keep-alive）就置 `stream.started`，之后的 5xx/断流/读超时不再重试；`Retry-After` 头没有被读取。
- 工具 handler 的普通 `Exception` 不是会话崩溃路径：应表现为工具结果 `TOOL_HANDLER_ERROR`（见 tool-coding-round），不应变成 `session_failed`。
- 已知入口错误已分类：失效连接 409、空消息 422、授权目标不存在 404、重复授权/拒绝 409；内部契约违规与未知异常原样暴露，不做全局兜底。
- `last_error` 不持久：重启 Web 后失败横幅消失，这是当前实现；"刷新后仍可见"只针对不重启的刷新。
- 重启后会话被自动 paused 不是卡死，点 `继续` 即可。
- Windows 上 Worker 在 Web 进程的 kill-on-close Job 里，随 Web 一起退出；仍用上面的进程查询确认没有残留。
- 这些测试会故意破坏环境，只在临时 `REDPANDA_HOME` 与临时工作区里做。
- 历史 live 状态：部分（2026-10-12，修复后）：模型失败可见与再试经隔离 API 注入认证失败验证（`.tools/verification/fixapi-20261012/`），kill -9 后工具不重复执行通过；真实 UI、真实供应商 429 未验。2026-10-18 按源码改写，未重新 live 验证；同日删除随文件服务移除的沙箱进程与挂载清理条目。
- 已知产品缺口（2026-10-18 Windows live）：命令运行中只杀会话 Worker 后，`last_error`、`unknown` 与新文案、无孤儿进程、文件未写出都符合；但会话随后永久卡死——`再试` 返回 200 而 `should_wake=false` 无动作，新消息只记 `user.message.received` 不产生回合，重启 Web 后也不自动 paused、不出现 `继续`。journal 末尾是没有 outcome 的 `command.attempt.started`。
