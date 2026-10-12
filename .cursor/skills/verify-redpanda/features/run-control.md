# 运行控制：暂停、中断、重试

控制正在执行的回合。来源：`Composer.tsx`（`暂停`/`继续`/`重试`/`退回编辑栏`）、`ExecutionProcess.tsx`（`中断`）、`app.py` 的 `/paused`、`/cancel`、`/retry`，命令中断见 `redpanda/tools/builtin/command_interrupts.py`。

## Sub-features

- `pause-resume`：`暂停` 让回合在安全点停下，`继续` 恢复（`POST .../paused`）。
- `cancel`：`中断` 取消当前回合（`POST .../cancel`）。
- `retry`：失败后 `重试`（`POST .../retry`）。
- `back-to-composer`：发送失败的消息 `退回编辑栏`，文字回到输入框。
- `interrupt-by-typing`：运行中再发一条消息会打断当前任务（终端入口明确如此提示）。

## How to get to it (user POV)

- 让模型执行长命令：`请用 execute_command 执行 sleep 60 && echo LATE > late.txt`（自动放行开启），运行中看输入框按钮。

## Driving it with the browser

- **中断**：运行中点 `中断`。(a) 60 秒后 `late.txt` 不存在；`ps` 中没有该会话遗留的 `sleep 60` 子进程。(b) `cancel` 2xx，步骤状态变为 `中断`/`失败`，侧栏回到 `空闲`。(c) `note.txt` 不变。(d) 对空闲会话调 `cancel` → 2xx 幂等或 4xx，不能改变会话历史。
- **暂停/继续**：(a) 暂停期间 SSE 不再出现新的工具调用；`继续` 后回合完成，`GET /api/sessions/<id>` 记录完整。(d) 暂停时刷新页面，按钮仍显示 `继续`。
- **重试**：把模型 key 临时改错制造失败 → `重试` 前恢复 key → (a) 新回合成功，失败回合不重复执行已完成的写操作（对比文件内容）。
- **打断**：运行中再发 `改为回复 INTERRUPTED_OK` → 原命令被打断、无 `late.txt`，回复含 `INTERRUPTED_OK`。

## Gotchas

- 进程级中断在 Windows 与 Linux 实现不同（`command_interrupts.py`），两边都要单独验。
- 需要 `live_model_ready`；`sleep` 时长保持短以免拖慢验证。
