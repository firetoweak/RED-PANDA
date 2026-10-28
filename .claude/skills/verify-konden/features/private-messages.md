# 私信

Agent 用 `send_message` 按全局名册中同事的名字发私信。参数是 `recipient`、`text`，回复已有交流时加 `reply_to`。发送成功只表示已经进入对方时间线，不等待回复；对方离线时会拉起它的 Worker。人的 `停止` 只打断当前轮；后续私信仍触发处理。来源：`konden/assistant/agent_messages.py`、`konden/assistant/host/agent_messages.py`、`konden/assistant/agents.py`。普通回复展示见 [reply-delivery.md](./reply-delivery.md)。

## Sub-features

- `deliver`：送达后对方 journal 有 `bot.private_message`，并因此开始一轮决策。
- `notice`：一段有回复关联的双方往来合成一张默认收起的卡片，显示对方、消息条数与最近时间；点击“与 <名字> 的交流”展开连续聊天记录，每条显示发送者、时间与 Markdown 正文，再次点击收起，支持 Enter / Space。独立的“打开 <名字> 的对话”入口负责跳转，刷新后记录仍可见。
- `after-interruption`：收件人当前轮被打断后，新的私信仍自动触发处理。
- `reject`：发给自己、名册中没有这个名字、引用不属于这两人，都是已知失败。
- `limit`：显式回复链超过 10 跳，或同一源头驱动的 Agent 消息超过 20 条，拒绝发送，并在发送方与人的这一轮里写明已达上限。

## How to get to it (user POV)

- 名册里的 `Ada` 与 `Lin`，无需关联代码项目。在 `Ada` 的 `消息` 里请它给 `Lin` 发私信。
- 打开 `Lin` 看这一轮是否开始。没有「发私信」按钮。

## Driving it with the browser

需要 `live_model_ready`。`code` 在发送方 journal 的命令结果里。已知失败（`ok: false`）的工具卡徽标仍是 `完成`，不要按 `失败` 去找。

- **送达**：请 `Ada` `send_message`，`recipient` 为 `Lin`，正文为一句确定的话，不带 `reply_to`。
  - (a) 结果 `code` 为 `DELIVERED`，`data.notice` 为 `已送达；不等待对方回复。`。`Lin` 的 journal 有 `bot.private_message`，`text` 是这句，`requests_decision` 为 true。双方的 `GET /api/sessions/<id>` 私信条目有相同的 `message_id` 和 `text`，方向分别为 `sent` 与 `received`。`Lin` 随后出现处理步骤与直接给人看的普通回复。
  - (b) 卡片头部默认 `aria-expanded=false`，点击“与 Lin 的交流”展开。让双方沿 `reply_to` 往返四条，原卡片显示“4 条消息”，展开后按“发送 → 回复 → 接着发送 → 再回复”连续显示；每条消息有发送者、时间和完整 Markdown。普通回复不会拆开这一段私信，打开的卡片收到后续消息时继续追加，保持展开；发送确认晚于回复时仍把被回复的消息放在前面。不同 Bot 和另起的话题各自成卡。再次点击、Enter / Space 可收起。名字带小头像和身份色，独立的“打开 <名字> 的对话”链接进入对方私聊。工具卡名为 `send_message`，徽标 `完成`。`Lin` 的普通结尾直接显示为给人的回复；它不会自动私信回 `Ada`。刷新后卡片重新收起，仍是一张卡片包含整段往来，不丢失或重复。
  - (c) 人检出目录的 `note.txt` 不变。`GET /api/inbox` 不因这条私信多出条目。发送方不等 `Lin` 回完才结束这次工具调用。展开与收起只改变阅读状态，不新增消息或推进会话；同一步向多人分别发信时，各自的连续交流保持独立。
  - (d) 见下方拒绝路径。
- **打断后的收件人**：在 `Lin` 执行时点 `停止`，再请 `Ada` 发另一句。
  - (a) 结果仍是 `DELIVERED`；`Lin` 自动处理新私信并产生新步骤，无需人点继续。
  - (b) `GET /api/inbox` 不产生会话停止通知；原有当前轮取消事实保留。
- **拒绝**：分别请 `Ada` 发给自己、发给名册不存在的名字，以及用一个不属于这两人的 `reply_to`。关联不同代码项目的 Bot 可以正常互发私信。
  - (a) 三次结果依次为 `SELF_MESSAGE` / `RECIPIENT_NOT_FOUND` / `INVALID_REPLY`，错误依次为 `不能给自己发私信。`、`没有这位同事。`、`引用的消息必须是你与这位同事之间的私信。`。收件人 journal 不新增 `bot.private_message`。
  - (b) 工具卡仍是 `send_message`，徽标 `完成`；不会为被拒绝的发送新增发出箭头标识。
  - (c) 已有私信不变。
  - (d) 空白 `recipient` 或 `text` → `INVALID_ARGUMENT`，同样不投递。
- **上限**：沿 `reply_to` 把同一条交流回复到第 11 跳（第 1 条不算跳），或让同一条人的消息驱动超过 20 条 Agent 消息（私信与群发言合计数）。
  - (a) 结果 `code` 为 `HOP_LIMIT_REACHED` 或 `ORIGIN_LIMIT_REACHED`，`error` 为 `这条交流已达上限，已通知人；需要继续时请在与人的对话中说明。`。发送方 journal 有 `bot.communication_limit_reached`，`requests_decision` 为 false。对方时间线没有这句被拒绝的正文。
  - (b) 发送方这一轮的步骤正文含 `与 <收件人> 的交流已达上限，需要你判断是否继续。`。
  - (c) `GET /api/inbox` 出现交流超限提示（`kind: limit`），没有批准按钮；人在发送方会话再发消息后消失，详见 inbox。不能再按「收件箱只有授权和控制提案」判定。
  - (d) 超限的那一次调用之后，对方 journal 的私信条数不增加。

## 外部会话协作（MCP）

当前外部会话的接入、持续收发、Web 导航和撤销验收见 [external-conversations.md](external-conversations.md)。独立验证是普通对话中的任务，由验证 Skill 约定方法、版本与证据，不再使用专门的验证通信入口。

以下日期记录属于旧版验证专用协议的历史结论，不能作为当前外部会话功能的验证证明。

历史：2026-10-23 的 SDK 往返、身份分流及 Bot 资料入口验证针对旧版协议，仅作为历史证据，不代表当前独立验证业务闭环已通过。

2026-10-24 增量通过：Windows、独立 home/端口，页面全局生成配置，无外部名称和固定交接人；同一真实 Codex CLI 会话自动领取身份、交付样例仓库版本，真实 DeepSeek 验证 Bot 通过内置 Playwright 独立发现正常路径自测遗漏的人数上限缺陷（14 例），原编码会话收到 issues_found 后提交修复，关联原请求以新版本复验，21 例后收到 passed。请求版本与实际测试版本一致，截图与矩阵真实落盘。Host 真正重启前后，两轮报告、回复关系及游标完全一致；390×844 全局设置撤销后旧凭据返回 401，1440×900 页面与刷新记录通过。Python 统一入口 856 项通过、3 项因 Windows 符号链接权限跳过；前端 187 项、类型检查与构建通过。证据在本地 `.tools/mcp-verification/evidence/`。业务闭环使用受控本机样例仓库，未据此宣称真实远端 PR、其他原生客户端、Linux、多机连接或空闲客户端自动唤醒已验证。

## Gotchas

- 私信只装配给有长期身份的 Agent。普通会话、子 Agent 没有 `send_message`。
- 上限数字是起步值：回复链 10 跳，同一源头 20 条。
- 历史 live：2026-10-20 Lin → Ada 私信送达、触发 Ada 普通回复，且无自动私信回发，证据见 [reply-delivery.md](./reply-delivery.md)。暂停、拒绝与交流上限尚未 live 重验。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：Lin 向 Ada 的 send_message 送达，Ada 无新用户输入即普通回复 PRIVATE_RECEIVED。Mo 给已暂停的 Ada 发私信，结果 paused=true，Ada 保持暂停且收件箱出现 paused 提示；人继续后才回复 PAUSED_RECEIVED。拒绝与交流上限未重验。

2026-10-20 Bot 解耦增量：隔离 home、零代码项目、真实 deepseek/deepseek-flash，通过个人目录写入并读回、方向声明、私信往返、两名成员群聊发言。证据路径与完整日志保存在本地验证记录；未据此宣称其余子功能或完整功能地图已重验。

2026-10-21 收发标识增量：隔离 Web、零代码项目、真实 DeepSeek，Atlas → Nova → Atlas 往返显示双向标识；点击名字、返回与刷新通过。1440×1004 明暗主题和 390×844 窄屏检查通过，无页面横向溢出。不存在的收件人返回 RECIPIENT_NOT_FOUND，标识数量不变。停止执行、其他拒绝与交流上限未在本轮 live 重验，完整证据保存在本地 `.tools/`。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：有项目 Lin 向无项目 Ada 送达 PRIVATE_MAP_OK，双方可见来源与跳转；暂停接收和交流上限未重验。

- 历史（会话暂停机制现已移除）：2026-10-23 执行停止更名验证：Windows Chrome、隔离 home、真实 DeepSeek；页面点击停止后 `session.execution_stopped=true`，同事私信仍送达但收件人不推进；待处理事项 `kind=stopped` 显示「执行已停止，有未处理的消息。继续后才会处理。」。点击继续后标记清除并处理输入。例行任务暂停状态未改，群聊与子任务页面本轮未重验。证据 `.tools/recovery-fixes/stop-rename-web/`。

- 2026-10-23 移除持久暂停验证：Windows Chrome、隔离 home、真实 DeepSeek：页面打断控制恢复 Bot，再从恢复验证 Bot 发新私信；结果为已送达、不等待回复，收件人自动产生新决策。没有会话停止字段、待处理停止项或继续入口。回复内容由模型决定；群消息打断后参与由进程回归覆盖，未重验群聊 Web。证据 `.tools/recovery-fixes/remove-stop-web/`。

2026-10-25 图标化增量：隔离 home、零代码项目、本地固定回复，通过页面驱动墨竹与松风的实际私信往返；双方的方向箭头、头像与名字、名字跳转及刷新保留通过。1064×956 明暗主题和 390×844 深色窄屏无横向溢出。既有外部协作者组件回归通过；真实供应商、拒绝、交流上限和打断后收信本轮未重验。证据 `.tools/pure-black-night/evidence/cooperation-plan/`。

2026-10-29 私信卡片增量：Windows Chrome、隔离 home/端口、零代码项目、受控本地模型，通过页面创建三位 Bot 并实际完成向两人分别发送与回复。四条私信在双方展示的原文一致；默认收起、点击与键盘展开收起、独立跳转、刷新历史通过，阅读操作不改变会话版本。1440px 明暗主题、768px、390px 和 320px 布局检查通过，长链接和代码块未造成页面横向溢出。Python 统一入口 930 项通过、7 项因平台限制跳过；前端统一入口 201 项、类型检查与构建通过。真实供应商、拒绝、交流上限和打断后收信本轮未重验。证据 `.tools/private-message-cards/evidence/private-messages/`。

2026-10-29 连续交流修正：替换上轮“每条私信一张卡片”的展示。同一回复链中的双方往来合为一张卡片，跨普通回复保持连续；不同同事与新话题不混合。全新隔离实例、受控本地模型，通过页面创建三位 Bot，完成一段四条往来和另一段两条往来；双方页面的四条消息原文与顺序一致，六条私信均有对应的收发记录。默认收起、独立跳转、键盘、刷新、明暗主题与 320/390/768/1440px 布局通过。发送确认晚到和展开后追加由已有前端回归保护。Python 统一入口 931 项通过、7 项因平台限制跳过；前端统一入口 201 项、类型检查与构建通过。真实供应商、拒绝、交流上限和打断后收信未重验。修正版关键截图及记录：`.tools/private-message-cards/evidence/corrected/`。
