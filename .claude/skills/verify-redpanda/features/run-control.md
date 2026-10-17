# 运行控制：停止、继续、重试

控制正在执行的回合。来源：`Composer.tsx`（运行中 `停止`、停下后 `继续`、`重试`/`退回编辑栏`）、`Conversation.tsx`（`停止` 接 `cancelTurn` → `POST .../cancel`，失败提示 `停止失败，请确认后端连接后重试。`）、`ExecutionProcess.tsx`（中断的调用显示 `中断`）、`command_execution.py`（`COMMAND_INTERRUPTED`）、`context/projection.py`（结果未知的提示）。用户侧没有「暂停」概念（20bf829 起）。

## Sub-features

- `stop`：运行中点 `停止` 结束当前回合（`POST .../cancel`）。
- `continue`：停下后同一位置出现 `继续`（`shouldWake && !running`，走 `POST .../retry`）。
- `retry`：失败后 `重试`（`POST .../retry`）。
- `back-to-composer`：发送失败的消息 `退回编辑栏`，文字回到输入框。
- `interrupt-by-typing`：运行中再发一条消息会打断当前任务。

## How to get to it (user POV)

- 让模型执行长命令：`请用 execute_command 执行 sleep 60 && echo LATE > late.txt`（自动放行开启），运行中看输入框按钮。

## Driving it with the browser

- **停止长命令**：运行中点 `停止`。(a) `late.txt` 不出现；`ps` 中没有该会话遗留的 `sleep` 进程（进程组被杀）。(b) `cancel` 2xx，该调用显示 `中断`，工具结果 `code=COMMAND_INTERRUPTED`、`ok=null`，`error` 说明因用户停止中断、结果未知、可能已部分执行，`hint` 提示续跑前先检查现场。(c) 其他文件不变。(d) 对空闲会话调 `cancel` → 幂等或 4xx，不改历史；后端断开时点停止 → 显示停止失败提示。
- **停止时正在写文件**：`write_file` / `apply_patch` 这类一次性写入会写完再停，(a) 文件是完整的新内容，不出现半截；之后的工具不再执行。
- **继续**：停下后点 `继续`。(a) 被中断的那条调用不会自动重跑（`late.txt` 不因继续而自动出现，除非模型检查后主动决定重做）。(b) 模型能看到「结果未知、先检查现场」的提示，并先读文件/查产物再决定。(d) 模型直接盲目重复有副作用的操作 → 记为模型行为观察，不算 runtime 问题。
- **重试**：把模型 key 临时改错制造失败 → `重试` 前恢复 key → (a) 新回合成功，已完成的写操作不重复执行。
- **打断**：运行中再发 `改为回复 INTERRUPTED_OK` → 原命令被打断、无 `late.txt`，回复含 `INTERRUPTED_OK`。

## 成功条件说明

- 提示只写 runtime 真知道的：执行被中断（用户停止/进程异常退出）、结果未知、可能已部分执行、继续前先检查现场。不要求 runtime 给出退出码、耗时等现场细节；原因未知时写「原因未知」，不猜。
- 中断后计划仍显示 `in_progress` 不算问题（见 work-plan）。

## Gotchas

- 进程级中断在 Windows 与 Linux 实现不同（`command_interrupts.py`），两边都要单独验。
- 需要 `live_model_ready`；`sleep` 时长保持短。

## Live status
- 部分（r2 2026-10-12，main@ee3e38d）：`cancel` 能中断 `sleep` 且 `late.txt` 不生成；当时界面只有「暂停」，暂停后命令仍跑完——已由 20bf829 改为 `停止`，待真实 UI 复跑
