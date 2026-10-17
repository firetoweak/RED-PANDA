# 计划、耗时与上下文用量

模型用 `update_plan` 维护计划；每轮显示耗时；输入框显示上下文用量，超过阈值自动 compact。来源：`redpanda/assistant/work_plan.py`、`WorkPlanPanel.tsx`、`workPlanPlacement.ts`、`TurnTiming.tsx`、`timelineTurns.ts`（`formatElapsedTime`）、`Composer.tsx`（用量环）、`Conversation.tsx`（compact 失败提示）、`ModelSettingsPage.tsx`（阈值字段）、`redpanda/assistant/compact/core.py`（`find_history`、`compact.window_rolled_over`、`latest_input_tokens`；`model_usage` 是写进 journal 的用量元数据键，不是模型工具）。

## Sub-features

- `plan-panel`：见 [work-plan.md](./work-plan.md)。
- `turn-timing`：运行中 `正在执行 · <时长>`，结束后 `耗时 <时长>`；悬停说明 `从本轮消息提交起计时，包含思考、工具执行和等待时间`。时长 < 60 秒为一位小数秒（如 `正在执行 · 3 秒`、`耗时 12.3 秒`），≥ 60 秒为 `1 分 5 秒`，满 1 小时为 `1 小时 0 分 5 秒`。
- `context-meter`：输入区右侧用量环，`role="img"`，`aria-label` 为 `<标题> · <已用> / <上限> tokens`。标题：有本窗口用量且有阈值时 `N% 已用`；本窗口还没有用量时 `暂无本窗口用量`；有用量但没有阈值时 `上下文用量`。token 数 ≥ 1000 写成 `64K` / `12.3K`，缺失写 `—`。
- `compact`：输入 tokens 达到模型的 `compact 触发值（输入 tokens）` 时整理上下文，之后模型可用 `find_history` 找回旧内容。

## How to get to it (user POV)

- 发 `请先用 update_plan 列三步计划，然后逐步完成，每步只回复一句话。`
- compact：在 `模型配置` 里 `编辑` 当前模型，改 `compact 触发值（输入 tokens）`（最小 1，新模型默认 200000）。

## Driving it with the browser

- **计划**：生命周期（创建、标记、更新、回收、完成）的完整判据见 [work-plan.md](./work-plan.md)，本条不再重复。
- **耗时**：
  - (a) 结束后的时长 = 本轮最终步骤 `occurredAt` − 用户消息 `occurredAt`（`turnElapsedMs`），可用 `GET /api/sessions/<id>` 的事件时间复算。
  - (b) 运行中显示 `正在执行 · N 秒` 并递增；结束显示 `耗时 N 秒`（或 `M 分 S 秒`），与复算值相差 < 0.1 秒（显示精度）。
  - (c) 刷新页面后已结束轮次的 `耗时` 不变（由事件时间算出，不是前端计时）。
  - (d) 点 `停止` 后计时必须停住：不再显示 `正在执行 · `；本轮没有带时间的最终步骤时（`turnElapsedMs` 为 `null`）整行不显示，而不是显示随墙钟增长的值。
- **上下文用量**：
  - (a) 发送一轮后 `GET /api/sessions/<id>` 的 `context_input_tokens` 非空，且随轮次增加。
  - (b) 用量环 `aria-label` 为 `N% 已用 · <context_input_tokens 格式化> / <阈值格式化> tokens`，N = round(已用/阈值×100)。
  - (c) 换会话模型后上限随 `GET /api/sessions/<id>/model` 的 `effective`（缺失时 `selected`）的 `compact_threshold_tokens` 变化。
  - (d) 新会话还没有模型回复时 → `暂无本窗口用量 · — / <阈值> tokens`。
- **compact**：把当前模型的 `compact 触发值（输入 tokens）` 调到很小（如 2000）并 `保存配置`，连续发几轮长消息。
  - (a) `GET /api/sessions/<id>` 的 `compact_count` 递增，`compact_phase` 经 `running` 到 `ready`；journal 中有 fact `compact.window_rolled_over`。
  - (b) compact 成功没有任何提示；刚滚到新窗口时 `context_input_tokens` 为 `null`，用量环先显示 `暂无本窗口用量 · — / 2K tokens`，下一次回复后才有新窗口的数值（低于 compact 前）。
  - (c) 之前的用户消息在界面上仍可见。
  - (d) compact 失败时 `compact_phase` 为 `failed`，输入区出现红色提示 `上下文整理失败。窗口仍超预算时会话无法继续推进。`。问模型最初说过的暗号 → 它应能经 `find_history` 答出；答不出记为问题。

## Gotchas

- 改 `compact 触发值（输入 tokens）` 会写 `config.json`，验证结束要改回或丢弃临时 home。
- 阈值字段不接受 0、负数和小数；表单校验见 model-settings。
- 没有「compact 成功」提示，判据只能看 `compact_count`、`compact_phase` 和 journal fact。
- 历史验证：2026-10-12（main@1a4790a）只经 API 核对通过，UI 未验。2026-10-18 按源码改写耗时、用量环与 compact 判据，未 live 复跑。
