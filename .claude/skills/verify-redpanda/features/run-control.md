# 运行控制：打断并发送、停止、继续与再试

控制正在执行的回合。来源：`Composer.tsx`（运行中 `停止`、`打断并发送`，停下后 `继续`）、`Conversation.tsx`（新消息接 `POST .../inputs`，运行中携带 `interrupt=true`；`停止` 接 `POST .../cancel`；模型失败横幅 `执行已停止` 带 `查看详情`/`再试`；请求失败横幅 `操作失败`）、`ErrorNotice.tsx`、`ExecutionProcess.tsx`（结果不确定的调用显示 `中断`）、`redpanda/tools/builtin/command_execution.py`（`COMMAND_INTERRUPTED`）、`redpanda/assistant/host/supervisor.py`（`cancel_turn`/`retry`/`accept_input`）。用户侧没有「暂停」按钮。

## Sub-features

- `stop`：运行中点 `停止`（aria-label `停止`）→ `POST /api/sessions/{id}/cancel`；会话被标为 paused，仍在前台等待的 `execute_command` 被中断（Linux 杀进程组，Windows 终止 Job）。已转后台的命令继续运行，需要用该条的 `停止命令` 或 `stop_command` 单独停止。
- `continue`：`继续`（aria-label `继续`）只在 `should_wake && lastError == null && !running` 时出现，走 `POST /api/sessions/{id}/retry`（paused 时先解除 paused 再推进）。
- `retry-after-failure`：模型请求失败后输入框上方出现红色横幅 `执行已停止。<失败原因首行>`，按钮 `查看详情`、`再试`；`再试` 同样走 `POST .../retry`。
- `interrupt-with-input`：运行中或等待子任务时发送新消息，按钮为 `打断并发送`；父当前执行取消后处理新消息，不等待或自动收回子任务，成功后不保持暂停。没有排队预览；文字与附件随同一消息提交。

## How to get to it (user POV)

- 确认 `自动放行写文件等工具` 已开启，请模型用 `execute_command`、`wait_seconds=30` 执行 `sleep 20 && echo LATE > late.txt`；Windows 用 `Start-Sleep -Seconds 20; Set-Content late.txt LATE`。在这 20 秒前台等待期间操作，避免转后台后才点停止。
- 模型失败：把 `connections.json` 中当前供应商的 key 临时改错后发一条消息。

## Driving it with the browser

- **停止长命令**：运行中点 `停止`。
  - (a) `late.txt` 不出现；进程表里没有该命令遗留的进程（只查本次命令，Windows 在本实例 web pid 的后代进程里查，见 crash-recovery 的进程查询）；`GET /api/sessions/<id>` 中 `session.paused` 为 true。
  - (b) `cancel` 返回 200；该调用徽标 `中断`；工具结果 `ok=null`、`code=COMMAND_INTERRUPTED`、`error` 为「命令被用户停止，可能已部分执行。」、`hint` 为「已捕获的输出不完整；续跑前先检查相关文件和外部状态，避免重复副作用。」，`data.output_is_result=false`。
  - (c) 工作区其他文件不变；被停之后不再发起新的工具调用。
  - (d) 对空闲会话调 `cancel` → 200，只把会话置为 paused，历史不变；后端断开时点 `停止` → 横幅 `操作失败。停止失败，请确认后端连接后重试。`。
- **停止时正在写文件**：`write_file`/`apply_patch` 是一次性写入，(a) 停止后文件是完整新内容或完整旧内容，不出现半截；(c) 之后的工具不再执行。
- **停止后的下一步**（2026-10-18 Windows 实跑）：停止一条运行中的命令后 `session.paused=true`、`should_wake=false`，**不出现 `继续`**；用户通过发送新消息继续，模型在新回合里能读到 `COMMAND_INTERRUPTED`。`继续` 只在会话被暂停且仍有待推进的决策时出现（典型是 Web 重启后被自动暂停的会话，见 crash-recovery）。
- **继续**：在出现 `继续` 的状态下点它。
  - (a) 被中断的调用不会自动重跑：`late.txt` 不因继续而自动出现，除非模型检查后主动重做（journal 里会出现一条新的 `execute_command` 调用）。
  - (b) `retry` 返回 200，`session.paused` 变回 false，模型的下一步能读到上面的 `COMMAND_INTERRUPTED` 结果。
  - (c) 历史消息条数不变，不新增用户消息。
  - (d) 有 `last_error` 时不显示 `继续`（只显示横幅 `再试`）；模型盲目重复有副作用的操作 → 记为模型行为观察，不算 runtime 问题。
- **失败后再试**：把 key 改错制造失败 → 恢复 key → 点 `再试`。
  - (a) 新回合成功；`GET` 中该用户消息只有一条，已完成的工具不重复执行。
  - (b) 失败时横幅文字以 `执行已停止。运行失败：` 开头；`查看详情` 打开错误详情页（标题 `执行已停止 · 错误详情`，有 `复制完整错误`）。
  - (c) 再试前 `session.last_error` 保持不变，刷新页面后横幅仍在。
  - (d) 后端断开时点 `再试` → 横幅 `操作失败。再试失败，请确认后端连接后重试。`。
- **运行中发送（打断）**：前台长命令运行中输入 `改为只回复 INTERRUPT_OK，不要重跑旧命令` 并回车或点 `打断并发送`。
  - (a) 旧命令产生 `COMMAND_INTERRUPTED`，`late.txt` 不出现；`GET` 中新用户消息只有一条，成功后 `session.paused=false`。
  - (b) 一次 `POST .../inputs` 携带 `interrupt=true`，没有先从浏览器请求 `cancel`，模型随后回复 `INTERRUPT_OK`，无需点继续。
  - (c) 已有历史和已发生的副作用保留；旧前台命令不自动重跑。子任务与独立后台命令继续运行，父根据新要求用 `reclaim` 或 `stop_command` 逐项停止；重投同一 `delivery_id` 不再打断新执行。
  - (d) 请求失败保留正文和附件并显示 `操作失败`；刷新后仅恢复文字，不自动发送。窄屏也能用回车发送，按钮可访问名称仍为 `打断并发送`。
- **等待子任务时插话**：委派任务仍在执行时，发送 `保留子任务，先回复 PARENT_INTERVENED`。
  - (a) 新消息入历史，`session.paused=false`；已有子任务未被标为收回，不等子任务完成就能处理新指令。
  - (b) 输入入口为 `打断并发送`，父回复 `PARENT_INTERVENED`；子任务仍能正常交回。
  - (c) 子任务去留由父的工具调用决定；独立 `停止` 仍收回全部未完成子任务。
  - (d) 改为要求父收回指定任务，核对实际 `reclaim` 调用及子会话出现结束事实并保留父给出的原因，不能把刚好自然结束记作收回通过。

## Gotchas

- 2026-10-22 打断并发送增量：Windows Chrome、隔离 home、真实 DeepSeek，1440×900 点击和 390×844 回车均在前台长命令运行中提交新指令。旧命令留下 `COMMAND_INTERRUPTED`，后续文件未生成；模型直接回复指定标记，`paused=false`，不需要继续。窄屏无页面横向溢出。重投不再次打断及冲突输入由进程测试验证；未验证 Linux、MCP 长调用或独立后台命令停止。完整证据留在本地 `.tools/interrupt-evidence/`。
- 2026-10-22 子任务保留增量（旧显式报告协议的历史记录，状态字段与报告方式已被自然回复协议替换）：同样使用隔离 Web、Windows Chrome 与真实 DeepSeek，等待子任务时从 `打断并发送` 提交保留要求，父约 1.5 秒后回复 `PARENT_INTERVENED`，同时 `has_active_subagents=true`、`paused=false`，子任务未被收回。另一次在新子任务运行中要求收回，父实际调用 `reclaim`，观察页与 GET 均显示已收回和用户给出的原因，`cancelled=true`。首次收回尝试撞上子自然结束，不计作收回通过。选择性收回一个、另一个正常报告和独立停止全部收回由进程测试验证；本轮未重验代码项目写入、后台命令停止与其他平台。证据留在 `.tools/interrupt-adjust-evidence/`。

- 进程级中断两平台实现不同：Linux 在 `redpanda/execution/process_posix.py`（`os.killpg(..., SIGKILL)`），Windows Host 命令在 `redpanda/execution/process_win.py`（`TerminateJobObject`），两边都要单独验。
- 输入框里的 `重试` 只属于附件上传失败的重试，与回合控制无关。
- 提示只写 runtime 真知道的：被用户停止、可能已部分执行、续跑前先检查现场。不要求给出退出码、耗时等细节。
- 中断后计划仍显示 `in_progress` 不算问题（见 work-plan）。
- 需要 `live_model_ready`；`sleep` 时长保持短。
- 历史 live 状态：部分（r2 2026-10-12，main@ee3e38d）：`cancel` 能中断 `sleep` 且 `late.txt` 不生成；当时界面只有「暂停」。2026-10-18 Windows live（deepseek-flash）：停止长命令（`COMMAND_INTERRUPTED`、`late.txt` 未生成、`paused=true`）、运行中发送的排队行与 `退回编辑栏` 通过；停止后不出现 `继续`（见上「停止后的下一步」）；失败后 `再试` 未驱动。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：20 秒前台命令中点停止，COMMAND_INTERRUPTED、paused=true，late.txt 未生成；后台命令运行时新消息得到回复，停止前台后后台仍继续。继续处理完成事实通过。单独停止后台时命令恰好自然结束，不能记作该路径通过；排队退回未重验。
