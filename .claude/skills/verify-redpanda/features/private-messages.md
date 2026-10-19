# 私信

Agent 用 `send_message` 按同项目同事的名字发私信。参数是 `recipient`、`text`，回复已有交流时加 `reply_to`。发送成功只表示已经进入对方时间线，不等待回复；对方离线时会拉起它的 Worker。人的 `停止` 不能被私信解开：消息仍接纳，但不产生新的 Step，要等人点 `继续`。来源：`redpanda/assistant/agent_messages.py`、`redpanda/assistant/host/agent_messages.py`、`redpanda/assistant/agents.py`。工作记录折叠见 [work-log.md](./work-log.md)。

## Sub-features

- `deliver`：送达后对方 journal 有 `coding_bot.private_message`，并因此开始一轮决策。
- `paused`：收件人 `session.paused` 为 true 时仍送达，结果说明对方暂停。
- `reject`：发给自己、同项目没有这个名字、人在别的项目、引用不属于这两人，都是已知失败。
- `limit`：显式回复链超过 10 跳，或同一源头驱动的 Agent 消息超过 20 条，拒绝发送，并在发送方与人的这一轮里写明已达上限。

## How to get to it (user POV)

- 同一项目的 `Ada` 与 `Lin`。在 `Ada` 的 `消息` 里请它给 `Lin` 发私信。
- 打开 `Lin` 看这一轮是否开始。没有「发私信」按钮。

## Driving it with the browser

需要 `live_model_ready`。`code` 在发送方 journal 的命令结果里。已知失败（`ok: false`）的工具卡徽标仍是 `完成`，不要按 `失败` 去找。

- **送达**：请 `Ada` `send_message`，`recipient` 为 `Lin`，正文为一句确定的话，不带 `reply_to`。
  - (a) 结果 `code` 为 `DELIVERED`，`data.paused` 为 false，`data.notice` 为 `已送达；不等待对方回复。`。`Lin` 的 journal 有 `coding_bot.private_message`，`text` 是这句，`requests_decision` 为 true。`GET /api/sessions/<Lin>` 随后出现一轮 `audience` 为 `log` 的步骤。
  - (b) `Ada` 的工具卡名为 `send_message`，徽标 `完成`。`Lin` 的时间线出现 `工作记录`，不是一条发给人的新回复。
  - (c) 人检出目录的 `note.txt` 不变。`GET /api/inbox` 不因这条私信多出条目。发送方不等 `Lin` 回完才结束这次工具调用。
  - (d) 见下方拒绝路径。
- **暂停的收件人**：在 `Lin` 的会话点 `停止`，确认 `session.paused` 为 true、按钮为 `继续`。再请 `Ada` 发另一句。
  - (a) 结果仍是 `DELIVERED`，`data.paused` 为 true，`notice` 为 `已送达；对方当前暂停，会在人让它继续后处理。`。`Lin` 的 journal 有这条私信，但在点 `继续` 之前 `items` 不新增步骤。
  - (b) `Lin` 保持 `继续`，侧栏不出现新的未读回复。
  - (c) `paused` 仍为 true。`GET /api/inbox` 不因此多一条「未处理私信」。
  - (d) 点 `继续` 之后，`Lin` 才处理这句，并出现 `audience` 为 `log` 的一轮。
- **拒绝**：分别请 `Ada` 发给自己、发给同项目不存在的名字、发给另一个项目里的同名 Agent，以及用一个不属于这两人的 `reply_to`。
  - (a) 四次结果依次为 `SELF_MESSAGE` / `RECIPIENT_NOT_FOUND` / `DIFFERENT_PROJECT` / `INVALID_REPLY`，错误依次为 `不能给自己发私信。`、`同项目中没有这位同事。`、`收件人不在同一个项目。`、`引用的消息必须是你与这位同事之间的私信。`。收件人 journal 不新增 `coding_bot.private_message`。
  - (b) 工具卡仍是 `send_message`，徽标 `完成`。
  - (c) 已有的私信和两边的暂停状态不变。
  - (d) 空白 `recipient` 或 `text` → `INVALID_ARGUMENT`，同样不投递。
- **上限**：沿 `reply_to` 把同一条交流回复到第 11 跳（第 1 条不算跳），或让同一条人的消息驱动超过 20 条 Agent 消息（私信与群发言合计数）。
  - (a) 结果 `code` 为 `HOP_LIMIT_REACHED` 或 `ORIGIN_LIMIT_REACHED`，`error` 为 `这条交流已达上限，已通知人；需要继续时请在与人的对话中说明。`。发送方 journal 有 `coding_bot.communication_limit_reached`，`requests_decision` 为 false。对方时间线没有这句被拒绝的正文。
  - (b) 发送方这一轮的步骤正文含 `与 <收件人> 的交流已达上限，需要你判断是否继续。`。
  - (c) `GET /api/inbox` 仍只有命令授权与控制提案，没有第三种条目。不要把这条写进收件箱验证。
  - (d) 超限的那一次调用之后，对方 journal 的私信条数不增加。

## Gotchas

- 私信只装配给有长期身份的 Agent。普通会话、子 Agent 没有 `send_message`。
- 上限数字是起步值：回复链 10 跳，同一源头 20 条。未 live 跑过。
