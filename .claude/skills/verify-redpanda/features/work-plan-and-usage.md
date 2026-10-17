# 计划、耗时与上下文用量

模型用 `update_plan` 维护计划；每轮显示耗时；输入框显示上下文用量，超过阈值自动 compact。来源：`redpanda/assistant/work_plan.py`、`WorkPlanPanel.tsx`、`workPlanPlacement.ts`、`TurnTiming.tsx`、`Composer.tsx`（`上下文用量`）、`redpanda/assistant/compact/core.py`（`find_history`；`model_usage` 是写进 journal 的用量元数据键，不是模型工具）。

## Sub-features

- `plan-panel`：见 [work-plan.md](./work-plan.md)。
- `turn-timing`：`正在执行 · ` 与 `耗时 `（说明：`从本轮消息提交起计时，包含思考、工具执行和等待时间`）。
- `context-meter`：`上下文用量` / `已用`，无数据时 `暂无本窗口用量`。
- `compact`：输入 tokens 达到模型的 `compact 触发值` 时整理上下文，之后可用 `find_history` 找回旧内容。

## How to get to it (user POV)

- 发 `请先用 update_plan 列三步计划，然后逐步完成，每步只回复一句话。`

## Driving it with the browser

- **计划**：生命周期（创建、标记、更新、回收、完成）的完整判据见 [work-plan.md](./work-plan.md)，本条不再重复。
- **耗时**：运行中显示 `正在执行 · Ns` 并递增；结束显示 `耗时 Ns`，与首尾事件时间差误差 < 2s。
- **上下文用量**：(a) 发送后 `已用` 数值非零且随轮次增加。
- **compact**：在 `模型配置` 把当前模型的 `compact 触发值` 调到很小（如 2000），连续发几轮长消息。(a) journal 中出现 compact 记录，`已用` 数值回落。(c) 之前的用户消息在界面上仍可见。(d) 问模型最初说过的暗号 → 它应能经 `find_history` 答出；答不出记为问题。

## Gotchas

- 改 `compact 触发值` 会写 `config.json`，验证结束要改回或丢弃临时 home。

## Live status
- 通过（2026-10-12，main@1a4790a）：只经 API 核对，UI 未验
