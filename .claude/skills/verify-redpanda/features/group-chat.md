# 群聊

项目的工作区操作菜单 `新建群聊` 选 2 到 6 个同项目 Agent 建群；群列在该项目的 Agent 下面（`群聊 <群名>`），点开是 `/groups/:groupId`。人在群里发言，可用 `点名` 选成员，不点名叫全体；Host 依次叫醒被点到的成员，成员用 `send_group_message` 发言或这次处理结束即算完成，然后叫下一位。`停止本轮` 清空待叫队列并打断当前被叫醒的成员。来源：`web/src/features/groups/`、`AgentSidebar.tsx`、`WorkspaceHeader.tsx`、`redpanda/channels/web/app.py`（`/api/groups*`）、`redpanda/assistant/host/agent_groups.py`。

## Sub-features

- `create`：`新建群聊` 对话框，`群名` 与 `成员`（只列同项目 Agent，少于 2 个时 `创建` 不可用）；创建后进入群页。
- `record`：群页列出发言；人的发言署名 `你`，Agent 的署名链到它的会话；点名显示为 `@名字` 徽标。
- `post`：`群消息` 输入框 + `发送到群`（Ctrl+Enter 也可）；`点名` 为空时叫全体成员。
- `calling`：Host 正在等某个成员时显示 `正在等 <名字> 回应…`。
- `stop`：`停止本轮` 只在一串调度进行中可用。
- `skip-paused`：被人 `停止` 因而暂停的成员直接跳过，不进入 `calling`，也不收到 `coding_bot.group_attention`。
- `supersede`：调度进行中再发一条人的消息，会换成新的一串；还没叫到的成员不再为旧消息被叫。
- `refresh`：SSE `group_changed`（只带 `group_id`）在建群、发言写入、换人、调度结束和停止时到达；别的标签页建的群也随之出现在侧栏，页面重读群数据，不需要手动重载。

## How to get to it (user POV)

- 侧栏项目名右侧 `<项目> 工作区操作` → `新建群聊`。
- 侧栏点 `群聊 <群名>`。

## Driving it with the browser

前置：同一项目里至少两个 Agent。成员发言需要 `live_model_ready`；没有可用模型时，成员每次被叫醒都以模型失败结束，Host 视为跳过并叫下一位，这足以验证顺序与停止，但不能验证发言。

- **建群**：菜单 `新建群聊` → 填 `群名`、在 `成员` 选两个 Agent → `创建`。
  - (a) `GET /api/groups` 多一项，`members` 为所选名字；每个成员的会话 journal 里有一条 `coding_bot.group_created`（不触发决策）。
  - (b) `POST /api/groups` 201；页面跳到 `/groups/<group_id>`；侧栏出现 `群聊 <群名>`。
  - (c) Agent 名册与各 Agent 状态不变；群不出现在 `/api/agents` 或 `/api/inbox`。
  - (d) 同项目同名再建 → 409 `项目内已有群：…`；只选一个成员 → `创建` 不可用（直接 POST 为 422）；成员不在本项目 → 404 `同项目中没有这位 Agent：…`。
- **人发言与依次叫醒**：在 `群消息` 输入后点 `发送到群`。
  - (a) `GET /api/groups/<id>` 的 `messages` 末尾是 `sender: null` 的这条；`calling` 依次出现被叫成员；成员 journal 里先有 `coding_bot.group_message`（不触发决策），被叫到时才有 `coding_bot.group_attention`。
  - (b) 页面出现 `你` 署名的消息，输入框清空，随后显示 `正在等 <名字> 回应…`。
  - (c) 发言者以外的成员各收到一份发言；未点名的成员在点名发言里不会被叫。
  - (d) 空白内容 → 422 `消息内容不能为空。`；点名非本群成员（直接 POST）→ 422 `只能点名这个群里的成员。`；不存在的群 → 404 `群不存在`。
- **成员发言与轮次**（需要 `live_model_ready`）：不点名发一条让大家各说一句。
  - (a) 每位成员被叫到后用 `send_group_message` 发言，群记录里出现以 Agent 名署名的消息；整轮无人发言或达到轮数上限后 `active` 为 false。
  - (b) 成员的会话里能看到 `send_group_message` 工具调用；群页随工具完成刷新。
  - (c) 一位成员发言后 Host 就叫下一位，不等它把剩下的活干完。
  - (d) 超出每次讨论的回复上限时，工具结果为 `GROUP_LIMIT_REACHED`，群记录不增加；轮数用完的发言照常送达，结果提示不再叫人。
  - (e) 成员被叫到后先发一条引用该消息的回执、继续工作、再发一条点名报告：报告会叫醒被点名的人，即使那次讨论的轮数已用完；它正忙时又被另一次讨论叫到，也不影响这份报告叫人，后面的成员不必等它忙完。
  - (f) 被叫成员的回应在 `reply_to` 引用了它被叫来回应的消息；没有引用的发言照常送达并叫它点名的人，但不算回应。
- **群级停止**：在 `正在等 … 回应…` 显示时点 `停止本轮`。
  - (a) `GET /api/groups/<id>` 的 `active` 为 false、`calling` 为 `[]`。
  - (b) `POST /api/groups/<id>/stop` 200；被叫成员当前这一轮被打断，但不进入暂停（会话 `paused=false`），之后给它发私信会照常叫醒它。
  - (c) 已经写入的群发言保留；被停止的这次讨论之后再有发言（例如被叫成员跑完后的报告）照常写入，但不再叫其他成员，工具结果提示已被人停止。
  - (d) 没有进行中的调度时按钮不可用；一串调度自然结束后（`active` 变为 false），页面不再显示 `正在等 …`，`停止本轮` 随即不可用，不需要刷新。
- **跳过暂停的成员**：先在一位成员的会话点 `停止`，确认 `session.paused` 为 true。再在群里发一条不点名的消息。
  - (a) `calling` 不含这位成员；它的 journal 没有这次消息的 `coding_bot.group_attention`。其他成员仍依次出现在 `calling`。
  - (b) 页面的 `正在等 …` 不出现这位成员的名字。
  - (c) 它保持暂停（`继续` 仍在）。`GET /api/inbox` 不因此多出条目。
  - (d) 只点名这位暂停成员时，没有人被叫。这一串收尾后 `active` 为 false、`calling` 为 `[]`。点 `继续` 不会补叫旧消息；之后的新发言才会再叫它。
- **新人发言换掉这一串**：在 `正在等 <名字> 回应…` 时再发一条不同的群消息。
  - (a) `messages` 末尾是新的这一条。旧的 `calling` 不再继续；随后 `calling` 里是新消息要点到的成员。旧消息里还没被叫到的成员，journal 没有旧消息的 `coding_bot.group_attention`。
  - (b) 页面先显示新的 `你` 署名消息，再显示新的 `正在等 …`。
  - (c) 已经写进群记录的旧发言还在。被打断的成员 `paused` 仍为 false。
  - (d) 空白的第二条 → 422 `消息内容不能为空。`，进行中的 `calling` 不变。

## Gotchas

- 调度状态只在内存：Web 服务重启后不恢复中断的轮次。
- 群名项目内唯一；第一版不能改成员或删群。
- 菜单、对话框与 `点名` 下拉依赖 `requestAnimationFrame`，浏览器面板隐藏时打不开，记为走不到。
- 2026-10-18 Windows live（隐藏面板，`live_model_ready` false，默认模型指向不存在的 Ollama）：侧栏群行、发言、依次叫醒（Kai 失败跳过后叫 Mo）、`停止本轮` 通过；建群对话框与 `点名` 下拉走不到，改经 `POST /api/groups` 建群；成员发言跳过。同轮发现：没有默认模型时新建 Agent 返回 500（首轮唤醒在 Worker 里抛出模型未配置），另行修复。
