---
version: 1
slug: "web-src-app-app-tsx"
primary_target: "web/src/app/App.tsx"
related_targets: ["web/src/styles/app.css","web/src/features/conversation/Conversation.tsx","web/src/features/groups/GroupChatPage.tsx"]
---

# Web 协作界面

Mode: Operate / Read。范围是现有 React 前端全部真实入口；以私聊、群聊、名册和模型配置为主，保留执行与资料功能。

## Direction contract

THESIS: 沿用用户已定稿的 A「轻盈协作」。通过同事和对话组织任务，群聊用于补读，私聊用于持续交流；不重做方向选择，不变成管理仪表盘。

OWN-WORLD: 白色或炭灰聊天背景、轻浅侧栏、灰蓝人类气泡、中性 Bot 气泡。身份色贯穿头像与点名。系统无衬线字体、细线图标、小圆角、克制边界。

STORY: 左侧找到 Bot 或群聊，中间读写，右侧资料按需打开。看历史收到消息只提示数量。引用原文后可返回；切换聊天保留位置和草稿。

FIRST VIEWPORT: 258px 左侧名册，顶部新建 Bot，其下搜索、对话过滤与分组；主区域 77px 标题栏，中间完整消息、底部输入。群聊输入紧凑，私聊输入充分。人的气泡整体靠右，Bot 靠左，正文同为左对齐。长气泡最多 72ch 正文加内边距，绝不因代码而加宽整条底色。代码在气泡内横向滚动。

FORM: 沿用已落地的 React 界面及气泡修正，不生成替代构图；seed: none (existing user-approved direction)。设计约束见 .superdesign/design-system.md，视觉基准以 primary_target 和 related_targets 中的实际前端为准。

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

验收：真实前后端、零项目可创建 Bot、群聊与私聊往返、长消息/代码/diff、群引用/可选点名、历史补读、新来信计数、独立草稿、模型配置、资料与执行入口、明暗配色、1440/1299/390 宽度。保留现有外部契约。实际前端的验证结论与未验证范围见对应 PR；完整本机证据留在 `.tools/` 和 `.impeccable/review/`。
