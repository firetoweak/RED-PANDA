# 运行控制：停止、继续、再试、排队消息

控制正在执行的回合。来源：`Composer.tsx`（运行中 `停止`、停下后 `继续`、运行中发送的排队行 `立即发送`/`退回编辑栏`）、`Conversation.tsx`（`停止` 接 `cancelTurn` → `POST .../cancel`；模型失败横幅 `执行已停止` 带 `查看详情`/`再试`；请求失败横幅 `操作失败`）、`ErrorNotice.tsx`、`ExecutionProcess.tsx`（结果不确定的调用显示 `中断`）、`redpanda/tools/builtin/command_execution.py`（`COMMAND_INTERRUPTED`）、`redpanda/assistant/host/supervisor.py`（`cancel_turn`/`retry`/`accept_input`）。用户侧没有「暂停」按钮。

## Sub-features

- `stop`：运行中点 `停止`（aria-label `停止`）→ `POST /api/sessions/{id}/cancel`；会话被标为 paused，仍在前台等待的 `execute_command` 被中断（Linux 杀进程组，Windows 终止 Job）。已转后台的命令继续运行，需要用该条的 `停止命令` 或 `stop_command` 单独停止。
- `continue`：`继续`（aria-label `继续`）只在 `should_wake && lastError == null && !running` 时出现，走 `POST /api/sessions/{id}/retry`（paused 时先解除 paused 再推进）。
- `retry-after-failure`：模型请求失败后输入框上方出现红色横幅 `执行已停止。<失败原因首行>`，按钮 `查看详情`、`再试`；`再试` 同样走 `POST .../retry`。
- `queue-while-running`：运行中再发消息**不打断**当前回合：消息变成输入框上方一行排队预览，带 `立即发送` 与 `退回编辑栏`（垃圾桶图标），当前回合结束（running 由真变假）时自动发出。

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
- **运行中发送（排队）**：运行中输入 `改为回复 QUEUED_OK` 并回车。
  - (a) 输入框清空，上方出现排队行显示该文字；此时 `GET` 里还没有这条用户消息，`sleep` 仍在跑。
  - (b) 当前回合结束后自动 `POST .../inputs`，回复含 `QUEUED_OK`；点 `立即发送` 则立刻发出（同样不调用 `cancel`）。
  - (c) 原命令不被打断：20 秒后 `late.txt` 出现。
  - (d) 点 `退回编辑栏` → 排队行消失，文字（与附件）回到输入框，不发送。

## Gotchas

- 进程级中断两平台实现不同：Linux 在 `redpanda/execution/process_posix.py`（`os.killpg(..., SIGKILL)`），Windows Host 命令在 `redpanda/execution/process_win.py`（`TerminateJobObject`），两边都要单独验。
- 输入框里的 `重试` 只属于附件上传失败的重试，与回合控制无关。
- 提示只写 runtime 真知道的：被用户停止、可能已部分执行、续跑前先检查现场。不要求给出退出码、耗时等细节。
- 中断后计划仍显示 `in_progress` 不算问题（见 work-plan）。
- 需要 `live_model_ready`；`sleep` 时长保持短。
- 历史 live 状态：部分（r2 2026-10-12，main@ee3e38d）：`cancel` 能中断 `sleep` 且 `late.txt` 不生成；当时界面只有「暂停」。2026-10-18 Windows live（deepseek-flash）：停止长命令（`COMMAND_INTERRUPTED`、`late.txt` 未生成、`paused=true`）、运行中发送的排队行与 `退回编辑栏` 通过；停止后不出现 `继续`（见上「停止后的下一步」）；失败后 `再试` 未驱动。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：20 秒前台命令中点停止，COMMAND_INTERRUPTED、paused=true，late.txt 未生成；后台命令运行时新消息得到回复，停止前台后后台仍继续。继续处理完成事实通过。单独停止后台时命令恰好自然结束，不能记作该路径通过；排队退回未重验。
