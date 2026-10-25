# Web 会话与聊天

从空首页直接新建一位 Bot，再在它的会话里发一条消息并看到回复。入口 `web_chat.py`，路由见 `web/src/app/App.tsx`，接口见 `konden/channels/web/app.py`、`konden/channels/web/channel.py`。创建 Agent 见 [session-sidebar.md](./session-sidebar.md)。

## Sub-features

- `home-empty`：`/` 由 `AgentHome` 处理，不自动新建会话。没有 Bot 时显示 `创建第一位 Bot` 和 `创建 Bot`；已有 Bot 时展示全部 Bot 列表、方向、状态与群聊。代码项目为可选关联。
- `open-agent`：按 session-sidebar 创建一个 Agent 后进入 `/sessions/<id>`。不再有草稿行或 `new agent` 标题。
- `send-message`：在 `消息` 输入框（占位 `输入消息，@ 选择 Bot…`）回车或点 `发送`，走 `POST /api/sessions/{id}/inputs`；Shift + Enter 换行。私聊输入区单行起步，加号在最前，模型、用量、发送／停止在最后，输入增高时向上扩到七行后内部滚动；底部不再单列工具栏与提示行。
- `mention`：私聊正文输入 `@` 显示全局 Bot 名册，输入名字前缀过滤，支持中文与带空格、符号的完整名字。方向键选择、Enter / Tab 补全或鼠标点选，Escape 关闭；候选打开时 Enter 只填名，不发送。私聊没有 `@all` 候选，填入名字不直接通知另一位 Bot，消息仍交给当前聊天的 Bot 处理。
- `reply-render`：回复以 Markdown 气泡渲染（`MarkdownMessage.tsx`），私聊不重复展示名字，头像靠消息末尾对齐；正文后过程栏最右侧只显示时间；用户消息也在正文外右对齐时间，不显示耗时。右键消息或触屏长按打开菜单，提供 `引用 <名字> 的消息` 和 `复制消息`，复制保留完整原文。桌面 Bot 资料栏默认展开，可用 `收起 Bot 资料` / `显示 Bot 资料` 切换；窄屏默认关闭抽屉。后台任务显示在资料中。人的气泡整体靠右、Bot 靠左，正文都左对齐，短消息收缩、长消息限制正文行宽，代码在内部滚动。思考与执行过程入口在回复之后并排紧凑展示，全程默认折叠，只由用户点击展开或收起，流式输出、等待授权和回合结束不改变用户选择；尚无正文时运行提示为 `思考中`。待授权事项仍通过输入区上方的待处理条操作。
- `windowed-history`：长会话只渲染阅读位置附近的轮次，向上滚动查看早期记录，`跳到最新` 恢复跟随输出；阅读历史期间收到新内容时按钮显示 `N 条新消息`。切换会话保留各自阅读位置。
- `spa-fallback`：未知前端路径回 `index.html`（`app.py` 的 `_FrontendFiles` 静态挂载）；未知 `/api/...` 保持 404 JSON，静态文件读取限制在构建目录内。

## How to get to it (user POV)

- 打开 `$KONDEN_VERIFY_URL/`，点 `创建 Bot`，无需登记项目或准备 Git 仓库。关联代码项目是单独的可选路径，见 workspace-management。
- 按 session-sidebar 新建 Agent，进入 `/sessions/<id>` 后在底部输入框打字发送。

## Driving it with the browser

- **打开 Agent（发消息之前）**：按 session-sidebar 直接创建一个 Bot，不要先发消息。
  - (a) 地址栏变为 `/sessions/<id>`；`GET /api/sessions/<id>` 返回 200，`GET /api/agents` 对应 Bot 的 `projects=[]`（未挂项目时）；`$KONDEN_HOME/runtime_sessions/` 下多出一个以该会话 id 的 sha256 命名、内含 `journal.sqlite` 的目录；`GET /api/agents` 里有它。
  - (b) `POST /api/agents` 返回 201，且发生在任何 `POST .../inputs` 之前；侧栏出现这个名字。刷新 `/` 停在欢迎页，不会再新建会话。
  - (c) `$SCRATCH_WORKSPACE/note.txt` 仍是 `verify`。
  - (d) 错误路径：打开 `/sessions/session-不存在` → 页面显示 `这个会话不存在。请从左侧新建或选择会话。`，`GET /api/sessions/session-不存在` 返回 404。
- **发首条消息**：发送 `请只回复 PING_OK`。
  - (a) `GET /api/sessions/<id>` 含这条用户消息；`GET /api/agents` 仍是这一个 Agent，不因发消息多出一个。
  - (b) `POST .../inputs` 返回 2xx；SSE 上有该会话的事件流（见 realtime-events）。
  - (c) `$SCRATCH_WORKSPACE/note.txt` 仍是 `verify`；`config.json`、`connections.json` 的 mtime 不变；会话 id 不变（首条消息不再建新会话）。
  - (d) 错误路径（直接 `curl -X POST .../inputs`，探测用）：`text` 为空串或全空格 → 422，`detail` 为 `消息内容不能为空。`；`connection_id` 为不存在的值 → 409，`detail` 为 `Web 连接已失效，请重新连接页面。`；两者都不是 500，journal 不增加用户消息。界面上这两种 `detail` 会原样显示在输入框上方。
- **回复**：(a) `GET /api/sessions/<id>` 里有用户消息与助手回复，回复正文含 `PING_OK`。(b) 界面出现助手气泡，右键菜单中的 `复制消息` 可点；有思考内容时出现可展开的 `思考` 块。(c) 会话数不因刷新页面而增加。(d) 模型 key 错误时，界面出现失败提示（`消息发送失败，请确认后端连接后重试。` 或回合失败卡片），journal 里有失败事实而不是空回复。
- **SPA 回落**：
  - (a)(b) `curl -i $KONDEN_VERIFY_URL/whatever/deep` 返回 200 且是 `index.html`。
  - (c) 已知 `/api/...` 路由行为不受回落影响（例如 `GET /api/workspaces` 仍是 JSON）。
  - (d) `curl -i $KONDEN_VERIFY_URL/api/不存在` 返回 404 JSON，而不是 `index.html`。用 `curl --path-as-is` 探测 `..` 或其编码变体，不应读到构建目录外预置的哨兵文件；只记录是否泄露，不保存无关文件内容。
- **长会话滚动**：准备隔离的多轮历史，打开会话后查看最近回复，再向上滚动到早期记录，最后点 `跳到最新`。
  - (a) `GET /api/sessions/<id>` 仍含完整历史；页面只挂载阅读位置附近的轮次，浏览后挂载数量不会随累计读过的历史增长。
  - (b) 早期记录能显示，离开底部后出现 `跳到最新` 或 `N 条新消息`，点击后回到最后一轮。
  - (c) 查看历史时新增回复和流式增长不改变阅读锚点；贴底时跟随内容增长；刷新后仍从最新记录打开。
  - (d) 检查包含代码块、不同长度回复的历史，连续翻阅与跳转后不得出现空白窗口或丢失历史。
  - 补充检查：在底部输入区左右空白处实际滚动鼠标，聊天均应翻动，滚动条延伸到聊天栏底部；输入多行长草稿后，输入框优先滚动自身文本，末条回复不被增高的输入区遮挡。

## Gotchas

- 点名补全验证：创建中文及带空格名字的两位 Bot，在私聊输入 `@`，确认完整名册与前缀筛选；用方向键和 Enter、Tab、鼠标分别补全，确认无误发送，名字后的正文可继续编辑。邮箱不弹候选，中文输入法选词不发送，引用预览不触发补全。发送后检查当前会话中的用户正文含完整名字，刷新与切换会话保留未发送草稿。复验群聊 `所有人 @all` 仍可补全。

- 首页不再自动建会话。Agent 在 `POST /api/agents` 成功时就进入 `GET /api/agents`，不必等第一条用户消息。不要把欢迎页上没有聊天记录误判为没创建。
- `live_model_ready` 为 false 时跳过发送与工具步骤，只验证无模型也可到达的页面入口；不要提交首条消息。
- `--workspace` 启动会预先登记工作区，跳过空首页路径；验证时不要用。
- `web/dist` 不存在时 `/` 不会有页面，只有 API。
- 历史产品缺口（2026-10-18 源码核对）：旧回落路由吞掉未知 API，并缺少静态目录约束。2026-10-20 源码核对：已改用 `_FrontendFiles(StaticFiles)`；`tests/channels/test_web.py` 的未知 API 与目录边界契约通过。当前版本尚待隔离 Web live 复验，不把测试结果记为 UI 通过。
- 历史 live 记录：部分（2026-10-12，修复后）：空消息 422、失效连接 409、模型失败可见与再试（隔离 API 通过）；页面观感待人工复跑。长会话滚动通过（2026-10-15）：隔离实例使用 200 轮合成历史和本地模拟流式服务，验证窗口挂载、早期记录、阅读锚点、流式跟随、上滑解除跟随与跳到最新；不代表真实模型调用已验证。2026-10-18 的修订只做源码核对，未 live 复跑。
- 历史观察（2026-10-18 Windows live）：模型接口曾每秒请求数次；当前 `Composer.tsx` 设置 `/model` 每 2 秒、`/model-settings` 每 5 秒轮询，当前频率尚未 live 复测。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：页面创建 Agent、真实用户输入、普通回复及刷新通过；长历史、错误输入与静态边界未重验。

2026-10-20 Bot 解耦增量：隔离 home、零代码项目、真实 deepseek/deepseek-flash，通过个人目录写入并读回、方向声明、私信往返、两名成员群聊发言。证据路径与完整日志保存在本地验证记录；未据此宣称其余子功能或完整功能地图已重验。

- 2026-10-20 手动展开增量验收：Windows 隔离实例、真实模型命令执行时，执行过程保持收起；手动展开后，命令与回合结束仍保持展开。历史执行过程和思考可手动展开、收起。待授权默认收起与断线后授权按钮状态由现有组件回归验证通过，本轮未在浏览器中重验待授权与思考流式输出。证据 `.tools/preview/evidence/manual-disclosures/`。

- 2026-10-21 私聊点名增量：Windows 隔离 Web、零项目、真实 DeepSeek，中文前缀、带空格名字、方向键与 Enter、Tab、点选、Escape 通过；补全后发送的正文保留准确名字，并实际私信送达指定 Bot。1440×1004 明暗主题、390×844 窄屏候选检查通过，无横向溢出。邮箱、输入法组合、光标中间替换和保留引用由组件测试验证；物理手机、其他浏览器未测。证据留在本地 `.tools/private-mentions/`。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：无项目 Bot 真实发送与回复、刷新历史通过；长历史、引用跳转与异常 API 未重验。

- 2026-10-23 时序增量：Windows Chrome、隔离实例、真实 DeepSeek，正文输出开始后刷新，150 行完整、单份显示并与 GET 对照一致；Ada → Lin → 群 → Lin → Ada 切换未串入正文，各自草稿保留。可控旧请求成功/失败及旧选择确认由组件/API 回归覆盖，未在浏览器人为延迟响应。证据 `.tools/verification/frontend-timing/evidence/`。
