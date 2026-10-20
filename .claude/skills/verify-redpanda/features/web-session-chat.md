# Web 会话与聊天

从空首页登记项目，在名册里新建一个 Agent，再在它的会话里发一条消息并看到回复。入口 `web_chat.py`，路由见 `web/src/app/App.tsx`，接口见 `redpanda/channels/web/app.py`、`redpanda/channels/web/channel.py`。创建 Agent 见 [session-sidebar.md](./session-sidebar.md)。

## Sub-features

- `home-empty`：`/` 与 `/workspaces/:workspaceId` 由 `AgentHome` 处理，不自动新建会话。没有项目时工作台显示 `从一个项目开始` 和按钮 `创建工作区`；已有项目时展示 Agent 卡片、负责方向、状态与新建入口。
- `open-agent`：按 session-sidebar 创建一个 Agent 后进入 `/sessions/<id>`。不再有草稿行或 `new agent` 标题。
- `send-message`：在 `消息` 输入框（占位 `描述你想完成的事，或拖入文件…`）回车或点 `发送`，走 `POST /api/sessions/{id}/inputs`；底部显示 Enter 发送、Shift + Enter 换行提示。
- `reply-render`：回复以 Markdown 气泡渲染（`MarkdownMessage.tsx`），Agent 身份显示在会话顶部，点击页头名字切换默认隐藏的右侧资料栏，不在每条回复中重复；悬停回复或键盘聚焦操作区可 `复制回复` 并查看时间与耗时。普通问答气泡分别靠右、靠左，直接间隔 8。思考与执行过程入口在回复之后，全程默认折叠，只由用户点击展开或收起，流式输出、等待授权和回合结束不改变用户选择；尚无正文时运行提示为 `思考中`。待授权事项通过输入区上方的待处理条操作。
- `windowed-history`：长会话只渲染阅读位置附近的轮次，向上滚动查看早期记录，`跳到最新` 恢复跟随输出。
- `spa-fallback`：未知前端路径回 `index.html`（`app.py` 的 `_FrontendFiles` 静态挂载）；未知 `/api/...` 保持 404 JSON，静态文件读取限制在构建目录内。

## How to get to it (user POV)

- 打开 `$REDPANDA_VERIFY_URL/`，没有工作区时点主区域 `创建工作区`（或侧栏 `新建工作区`）建一个指向 `$SCRATCH_WORKSPACE` 的工作区（见 workspace-management）。`$SCRATCH_WORKSPACE` 须已是带首个提交的 Git 仓库。
- 按 session-sidebar 新建 Agent，进入 `/sessions/<id>` 后在底部输入框打字发送。

## Driving it with the browser

- **打开 Agent（发消息之前）**：工作区建好后按 session-sidebar 创建一个 Agent，不要先发消息。
  - (a) 地址栏变为 `/sessions/<id>`；`GET /api/sessions/<id>` 返回 200，`workspace_id` 等于刚建的工作区；`$REDPANDA_HOME/runtime_sessions/` 下多出一个以该会话 id 的 sha256 命名、内含 `journal.sqlite` 的目录；`GET /api/agents` 里有它。
  - (b) `POST /api/agents` 返回 201，且发生在任何 `POST .../inputs` 之前；侧栏出现这个名字。刷新 `/` 停在欢迎页，不会再新建会话。
  - (c) `$SCRATCH_WORKSPACE/note.txt` 仍是 `verify`。
  - (d) 错误路径：打开 `/sessions/session-不存在` → 页面显示 `这个会话不存在。请从左侧新建或选择会话。`，`GET /api/sessions/session-不存在` 返回 404。
- **发首条消息**：发送 `请只回复 PING_OK`。
  - (a) `GET /api/sessions/<id>` 含这条用户消息；`GET /api/agents` 仍是这一个 Agent，不因发消息多出一个。
  - (b) `POST .../inputs` 返回 2xx；SSE 上有该会话的事件流（见 realtime-events）。
  - (c) `$SCRATCH_WORKSPACE/note.txt` 仍是 `verify`；`config.json`、`connections.json` 的 mtime 不变；会话 id 不变（首条消息不再建新会话）。
  - (d) 错误路径（直接 `curl -X POST .../inputs`，探测用）：`text` 为空串或全空格 → 422，`detail` 为 `消息内容不能为空。`；`connection_id` 为不存在的值 → 409，`detail` 为 `Web 连接已失效，请重新连接页面。`；两者都不是 500，journal 不增加用户消息。界面上这两种 `detail` 会原样显示在输入框上方。
- **回复**：(a) `GET /api/sessions/<id>` 里有用户消息与助手回复，回复正文含 `PING_OK`。(b) 界面出现助手气泡，`复制回复` 可点；有思考内容时出现可展开的 `思考` 块。(c) 会话数不因刷新页面而增加。(d) 模型 key 错误时，界面出现失败提示（`消息发送失败，请确认后端连接后重试。` 或回合失败卡片），journal 里有失败事实而不是空回复。
- **SPA 回落**：
  - (a)(b) `curl -i $REDPANDA_VERIFY_URL/whatever/deep` 返回 200 且是 `index.html`。
  - (c) 已知 `/api/...` 路由行为不受回落影响（例如 `GET /api/workspaces` 仍是 JSON）。
  - (d) `curl -i $REDPANDA_VERIFY_URL/api/不存在` 返回 404 JSON，而不是 `index.html`。用 `curl --path-as-is` 探测 `..` 或其编码变体，不应读到构建目录外预置的哨兵文件；只记录是否泄露，不保存无关文件内容。
- **长会话滚动**：准备隔离的多轮历史，打开会话后查看最近回复，再向上滚动到早期记录，最后点 `跳到最新`。
  - (a) `GET /api/sessions/<id>` 仍含完整历史；页面只挂载阅读位置附近的轮次，浏览后挂载数量不会随累计读过的历史增长。
  - (b) 早期记录能显示，离开底部后出现 `跳到最新`，点击后回到最后一轮。
  - (c) 查看历史时新增回复和流式增长不改变阅读锚点；贴底时跟随内容增长；刷新后仍从最新记录打开。
  - (d) 检查包含代码块、不同长度回复的历史，连续翻阅与跳转后不得出现空白窗口或丢失历史。
  - 补充检查：在底部输入区左右空白处实际滚动鼠标，聊天均应翻动，滚动条延伸到聊天栏底部；输入多行长草稿后，输入框优先滚动自身文本，末条回复不被增高的输入区遮挡。

## Gotchas

- 首页不再自动建会话。Agent 在 `POST /api/agents` 成功时就进入 `GET /api/agents`，不必等第一条用户消息。不要把欢迎页上没有聊天记录误判为没创建。
- `live_model_ready` 为 false 时跳过发送与工具步骤，只验证无模型也可到达的页面入口；不要提交首条消息。
- `--workspace` 启动会预先登记工作区，跳过空首页路径；验证时不要用。
- `web/dist` 不存在时 `/` 不会有页面，只有 API。
- 历史产品缺口（2026-10-18 源码核对）：旧回落路由吞掉未知 API，并缺少静态目录约束。2026-10-20 源码核对：已改用 `_FrontendFiles(StaticFiles)`；`tests/channels/test_web.py` 的未知 API 与目录边界契约通过。当前版本尚待隔离 Web live 复验，不把测试结果记为 UI 通过。
- 历史 live 记录：部分（2026-10-12，修复后）：空消息 422、失效连接 409、模型失败可见与再试（隔离 API 通过）；页面观感待人工复跑。长会话滚动通过（2026-10-15）：隔离实例使用 200 轮合成历史和本地模拟流式服务，验证窗口挂载、早期记录、阅读锚点、流式跟随、上滑解除跟随与跳到最新；不代表真实模型调用已验证。2026-10-18 的修订只做源码核对，未 live 复跑。
- 历史观察（2026-10-18 Windows live）：模型接口曾每秒请求数次；当前 `Composer.tsx` 设置 `/model` 每 2 秒、`/model-settings` 每 5 秒轮询，当前频率尚未 live 复测。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：页面创建 Agent、真实用户输入、普通回复及刷新通过；长历史、错误输入与静态边界未重验。

- 2026-10-20 手动展开增量验收：Windows 隔离实例、真实模型命令执行时，执行过程保持收起；手动展开后，命令与回合结束仍保持展开。历史执行过程和思考可手动展开、收起。待授权默认收起与断线后授权按钮状态由现有组件回归验证通过，本轮未在浏览器中重验待授权与思考流式输出。证据 `.tools/preview/evidence/manual-disclosures/`。
