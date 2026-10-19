# 工作记录折叠

长期 Agent 每一轮的结尾文字都是工作记录，不是给人的回复：时间线里收成一行 `工作记录`，点开才看到全文与工具。人只看到 Agent 用 `message_user` 发给他的消息，照常显示为回复，包括回答人直接提的问题。普通会话不受影响，结尾直接是回复。来源：`redpanda/assistant/audience.py`（`audience`）、`redpanda/assistant/conversations.py`（步骤项的 `audience`）、`redpanda/assistant/host/supervisor.py`（实时正文、预览、思考按会话是否为 Agent 给出 audience）、`redpanda/channels/web/hub.py`（`preview.started` / `thinking.started` / `output_final` 带 `audience`）、`web/src/features/conversation/timelineTurns.ts`、`WorkLog.tsx`。

## Sub-features

- `fold`：连续的 `audience=log` 步骤收成一行按钮，可访问名称以 `工作记录` 开头，后接开始时间与一行摘要（最后一步的首行）；`aria-expanded=false`。点开显示执行过程、思考与结尾全文。
- `live`：工作记录这一轮在流式输出时同样只显示折叠行（左侧转圈），不先以回复气泡弹出再在刷新后折叠。
- `no-attention`：工作记录的结尾文字不增加未读，不算「新回复」。
- `message-user`：Agent 在工作记录这一轮里调 `message_user` 发出的消息是普通回复气泡，不在折叠里。
- `needs-action`：工作记录里有待授权的工具时自动展开。

## How to get to it (user POV)

- 在一个 Agent 的会话里直接问它一个问题：它的回答经 `message_user` 显示为回复气泡，这一轮的结尾文字折成 `工作记录`。
- 同项目建两个 Agent `Ada` 与 `Lin`。在 `Lin` 的会话发 `请用 send_message 给 Ada 发「看一下 note.txt，然后回复我」`。打开 `Ada` 的会话：这一轮是工作记录。
- 例行任务到点（见 routines）开始的一轮同样是工作记录。

## Driving it with the browser

- **折叠**：(a) `GET /api/sessions/<Ada>` 中 Ada 的每个步骤项都是 `audience="log"`，人发起的轮次也一样；`message_user` 发出的消息项是 `audience="user"`。(b) 时间线出现按钮 `工作记录 …`，结尾全文不在页面上；点开后出现。(c) 人发起的那一轮里，回答人的 `message_user` 消息是回复气泡，不被折进去。(d) 刷新页面后折叠状态一致（默认收起）。普通会话的回复不折叠。
- **实时**：在 Ada 的工作记录这一轮运行时打开 Ada 的会话。(a) SSE `preview.started` / `thinking.started` / `output_final` 的 `audience` 为 `log`。(b) 只看到转圈的 `工作记录` 行，没有流式回复气泡。(c) 收尾后不需要刷新，样子不变。(d) 停在别的 Agent 页面时，Ada 不因这一轮新增未读。
- **message_user**：在 Lin 的消息里要求 Ada 「用 message_user 告诉人结论」。(a) `GET` 中该消息是单独一项 `audience="user"`，`output_id` 是那次调用的 `command_id`。(b) 时间线里该消息是回复气泡，位于折叠行之后。(c) 工作记录本身仍折叠。(d) `message_user` 被拒（如空文本）时它只是工具记录，留在折叠里。

## Gotchas

- 需要 `live_model_ready`，且要两个 Agent 互发私信，耗时较长。
- audience 只看会话是不是长期 Agent，不看这一轮由什么开始。模型若没有用 `message_user` 回答人，人只会在工作记录里看到它的话，这是模型行为问题，记为观察结果。
- 历史 live 状态：部分（2026-10-19 Windows 隔离实例 + deepseek-flash，Playwright 驱动）：同事私信开始的一轮折叠为 `工作记录` 行、点开见全文；运行中只见转圈的折叠行、回复气泡数不变；SSE 中这一轮的 `preview.started`/`thinking.started`/`output_final` 为 `log`，`message_user` 的 `output_final` 为 `user` 并显示为回复。群与例行任务触发的一轮未 live 观察。
- 相邻的工作记录合成一行：两次同事来信之间没有人的消息或给人的回复时，新的一轮接在同一行里。
