# 计划、消息时间与上下文用量

模型用 `update_plan` 维护计划；消息尾部显示时间；输入框显示上下文用量，超过阈值自动 compact。来源：`redpanda/assistant/work_plan.py`、`WorkPlanPanel.tsx`、`workPlanPlacement.ts`、`messageTime.ts`、`Composer.tsx`（用量环）、`Conversation.tsx`（compact 失败提示）、`ModelSettings.tsx`（阈值字段）、`redpanda/assistant/compact/core.py`（`find_history`、`compact.window_rolled_over`、`latest_input_tokens`；`model_usage` 是写进 journal 的用量元数据键，不是模型工具）。

## Sub-features

- `plan-panel`：见 [work-plan.md](./work-plan.md)。
- `message-time`：用户消息外侧与 Bot 回复后的过程栏显示消息时间；悬停可看完整日期时间。不再显示每轮耗时或运行秒数。
- `context-meter`：输入区右侧用量环，`role="img"`，`aria-label` 为 `<标题> · <已用> / <上限> tokens`。标题：有本窗口用量且有阈值时 `N% 已用`；本窗口还没有用量时 `暂无本窗口用量`；有用量但没有阈值时 `上下文用量`。token 数 ≥ 1000 写成 `64K` / `12.3K`，缺失写 `—`。
- `compact`：输入 tokens 达到模型的 `compact 触发值（输入 tokens）` 时整理上下文，之后模型可用 `find_history` 找回旧内容。

## How to get to it (user POV)

- 发 `请先用 update_plan 列三步计划，然后逐步完成，每步只回复一句话。`
- compact：在 `模型配置` 里 `编辑` 当前模型，改 `compact 触发值（输入 tokens）`（最小 1，新模型默认 200000）。

## Driving it with the browser

- **计划**：生命周期（创建、标记、更新、回收、完成）的完整判据见 [work-plan.md](./work-plan.md)，本条不再重复。
- **消息时间**：(a) 用 `GET /api/sessions/<id>` 的事件时间对照。(b) 用户消息外侧和 Bot 过程栏右侧有时间。(c) 刷新后仍来自同一事件；不能再按旧版 `耗时` 文案或墙钟差判断。(d) 无时间的临时流式内容不应被当成已结束轮次。
- **上下文用量**：
  - (a) 发送一轮后 `GET /api/sessions/<id>` 的 `context_input_tokens` 与当前窗口最近一次模型输入用量一致；不是累计计数，不要求随轮次单调增加。
  - (b) 用量环 `aria-label` 为 `N% 已用 · <context_input_tokens 格式化> / <阈值格式化> tokens`，N = round(已用/阈值×100)。
  - (c) 换会话模型后上限随 `GET /api/sessions/<id>/model` 的 `effective`（缺失时 `selected`）的 `compact_threshold_tokens` 变化。
  - (d) 新会话还没有模型回复时 → `暂无本窗口用量 · — / <阈值> tokens`。
- **compact**：把当前模型的 `compact 触发值（输入 tokens）` 调到很小（如 2000），先点 `加入待保存配置` 再 `保存配置`，连续发几轮长消息。
  - (a) `GET /api/sessions/<id>` 的 `compact_count` 递增，journal 中有 fact `compact.window_rolled_over`；`compact_phase` 可短暂经过 `running`、`ready`，完成后回到 `null`，不要求最终保持 `ready`。
  - (b) compact 成功没有任何提示；刚滚到新窗口时 `context_input_tokens` 为 `null`，用量环先显示 `暂无本窗口用量 · — / 2K tokens`，下一次回复后才有新窗口的数值；不保证新窗口的输入用量一定低于整理前。
  - (c) 之前的用户消息在界面上仍可见。
  - (d) compact 失败时 `compact_phase` 为 `failed`，输入区出现红色提示 `上下文整理失败。窗口仍超预算时会话无法继续推进。`。先修复失败原因并恢复会话，再问模型最初说过的暗号；成功整理后的模型应能经 `find_history` 找回。失败仍超预算时不能要求模型继续回答。

## Gotchas

- 改 `compact 触发值（输入 tokens）` 会写 `config.json`，验证结束要改回或丢弃临时 home。
- 阈值字段不接受 0、负数和小数；表单校验见 model-settings。
- 没有「compact 成功」提示，判据只能看 `compact_count`、`compact_phase` 和 journal fact。
- 历史验证：2026-10-12（main@1a4790a）只经 API 核对通过，UI 未验。2026-10-18 按源码改写耗时、用量环与 compact 判据，未 live 复跑。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：真实回合的上下文用量与耗时可见，API context_input_tokens 有值；compact 阶段和计数未触发，不能据此标为通过。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：真实回合时间与上下文用量环、200K 阈值和 GET 一致，不再有轮次耗时；compact 未触发。
