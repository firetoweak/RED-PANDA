# Web 会话与聊天

从空首页新建工作区，自动落到该工作区的草稿会话，在配置了模型时发一条消息并看到回复。入口 `web_chat.py`，路由见 `web/src/app/App.tsx`，接口见 `redpanda/channels/web/app.py`、`redpanda/channels/web/channel.py`。

## Sub-features

- `home-empty`：`/` 由 `DraftRedirect`（`web/src/features/sessions/DraftRedirect.tsx`）处理；未连上时显示 `正在连接…`，有工作区时显示 `正在打开会话…`；一个工作区都没有时主区域显示欢迎图、文字 `先选择一个文件夹作为工作区，小熊猫会在这里与你处理文件和任务。` 和按钮 `创建工作区`（打开 `新建工作区` 弹窗）。
- `draft-session`：`/` 或 `/workspaces/:workspaceId` 一旦有连接和目标工作区，立即 `POST /api/sessions`（`web/src/features/sessions/bindDraft.ts`）建一个草稿会话并 `replace` 跳到 `/sessions/<id>`，不等首条消息；该工作区已有草稿时直接跳到已有草稿，不再新建。侧栏在该工作区组顶部显示草稿行，标题 `new agent`（`workspaces.ts` 的 `DRAFT_TITLE`）。建会话失败时显示 `无法打开会话`、错误文字和 `重试`。
- `send-message`：在 `消息` 输入框（占位 `输入消息，Enter 发送，可拖入文件或粘贴图片`）回车或点 `发送`，走 `POST /api/sessions/{id}/inputs`。
- `reply-render`：回复以 Markdown 渲染（`MarkdownMessage.tsx`），可 `复制回复`；思考内容折叠块标题为 `思考`（`ThinkingBlock.tsx`）；尚无正文时运行提示为 `思考中`（`Conversation.tsx` 的 `RunningHint`、`stepHeading.ts`）。
- `windowed-history`：长会话只渲染阅读位置附近的轮次，向上滚动查看早期记录，`跳到最新` 恢复跟随输出。
- `spa-fallback`：未知前端路径回 `index.html`（`app.py` 的 `/{path:path}`）；未知 `/api/...` 应是 404 JSON（当前不满足，见 Gotchas）。

## How to get to it (user POV)

- 打开 `$REDPANDA_VERIFY_URL/`，没有工作区时点主区域 `创建工作区`（或侧栏 `新建工作区`）建一个指向 `$SCRATCH_WORKSPACE` 的工作区（见 workspace-management）。
- 页面自动跳到该工作区的草稿会话 `/sessions/<id>`，在底部输入框打字发送。

## Driving it with the browser

- **打开草稿（发消息之前）**：工作区建好后不做任何输入，观察跳转。
  - (a) 地址栏变为 `/sessions/<id>`；`GET /api/sessions/<id>` 返回 200，`workspace_id` 等于刚建的工作区；`$REDPANDA_HOME/runtime_sessions/` 下多出一个以会话 id 的 sha256 命名、内含 `journal.sqlite` 的目录；`GET /api/sessions` 列表里**还没有**它（列表只收已有用户消息的会话，`redpanda/assistant/conversations.py` `list_sessions`）。
  - (b) 浏览器网络面板里 `POST /api/sessions` 返回 201，且发生在任何 `POST .../inputs` 之前；侧栏该工作区组顶部出现 `new agent` 行。
  - (c) 刷新 `/`、或再次点同一工作区的 `在 <名称> 新建会话`，回到同一个草稿 id，不再产生第二个 `POST /api/sessions`（草稿归属记在浏览器 `localStorage` 的 `redpanda.draftSessions`，`web/src/app/store.ts`）；`$SCRATCH_WORKSPACE/note.txt` 仍是 `verify`。
  - (d) 错误路径：打开 `/sessions/session-不存在` → 页面显示 `这个会话不存在。请从左侧新建或选择会话。`，`GET /api/sessions/session-不存在` 返回 404。
- **发首条消息**：发送 `请只回复 PING_OK`。
  - (a) `GET /api/sessions` 此时出现该会话，`workspace_id` 正确；`GET /api/sessions/<id>` 含这条用户消息。
  - (b) `POST .../inputs` 返回 2xx；SSE 上有该会话的事件流（见 realtime-events）。
  - (c) `$SCRATCH_WORKSPACE/note.txt` 仍是 `verify`；`config.json`、`connections.json` 的 mtime 不变；会话 id 不变（首条消息不再建新会话）。
  - (d) 错误路径（直接 `curl -X POST .../inputs`，探测用）：`text` 为空串或全空格 → 422，`detail` 为 `消息内容不能为空。`；`connection_id` 为不存在的值 → 409，`detail` 为 `Web 连接已失效，请重新连接页面。`；两者都不是 500，journal 不增加用户消息。界面上这两种 `detail` 会原样显示在输入框上方。
- **回复**：(a) `GET /api/sessions/<id>` 里有用户消息与助手回复，回复正文含 `PING_OK`。(b) 界面出现助手气泡，`复制回复` 可点；有思考内容时出现可展开的 `思考` 块。(c) 会话数不因刷新页面而增加。(d) 模型 key 错误时，界面出现失败提示（`消息发送失败，请确认后端连接后重试。` 或回合失败卡片），journal 里有失败事实而不是空回复。
- **SPA 回落**：
  - (a)(b) `curl -i $REDPANDA_VERIFY_URL/whatever/deep` 返回 200 且是 `index.html`。
  - (c) 已知 `/api/...` 路由行为不受回落影响（例如 `GET /api/workspaces` 仍是 JSON）。
  - (d) `curl -i $REDPANDA_VERIFY_URL/api/不存在` 应返回 404 JSON，而不是 `index.html`（当前源码不满足，见 Gotchas）。
- **长会话滚动**：准备隔离的多轮历史，打开会话后查看最近回复，再向上滚动到早期记录，最后点 `跳到最新`。
  - (a) `GET /api/sessions/<id>` 仍含完整历史；页面只挂载阅读位置附近的轮次，浏览后挂载数量不会随累计读过的历史增长。
  - (b) 早期记录能显示，离开底部后出现 `跳到最新`，点击后回到最后一轮。
  - (c) 查看历史时新增回复和流式增长不改变阅读锚点；贴底时跟随内容增长；刷新后仍从最新记录打开。
  - (d) 检查包含代码块、不同长度回复的历史，连续翻阅与跳转后不得出现空白窗口或丢失历史。
  - 补充检查：在底部输入区左右空白处实际滚动鼠标，聊天均应翻动，滚动条延伸到聊天栏底部；输入多行长草稿后，输入框优先滚动自身文本，末条回复不被增高的输入区遮挡。

## Gotchas

- 草稿会话在打开页面时就已建好（`POST /api/sessions` 201），只是在首条用户消息之前不出现在 `GET /api/sessions`，这是设计不是丢失；不要把「列表里没有」误判为「没建会话」，也不要把「打开页面就建了会话」误判为多建。
- `live_model_ready` 为 false 时只验到发首条消息、journal 出现用户消息为止。
- `--workspace` 启动会预先登记工作区，跳过空首页路径；验证时不要用。
- `web/dist` 不存在时 `/` 不会有页面，只有 API。
- 已知产品缺口（2026-10-18 源码核对）：`app.py` 的 `@app.get("/{path:path}")` 不排除 `api/` 前缀，未知 `GET /api/...` 返回 200 + `index.html`，不是 404 JSON。判据保持 404 JSON 的预期。
- 已知产品缺口（2026-10-18 源码核对，安全）：同一回落路由把原始请求路径直接拼到静态目录上再判断 `is_file()`，不做规范化或目录约束，带 `..` 段的请求可以逃出 `web/dist` 读取其他文件。修复另行跟踪；验证时只记录现象，不在证据里保存读出的文件内容。
- 历史 live 记录：部分（2026-10-12，修复后）：空消息 422、失效连接 409、模型失败可见与再试（隔离 API 通过）；页面观感待人工复跑。长会话滚动通过（2026-10-15）：隔离实例使用 200 轮合成历史和本地模拟流式服务，验证窗口挂载、早期记录、阅读锚点、流式跟随、上滑解除跟随与跳到最新；不代表真实模型调用已验证。2026-10-18 的修订只做源码核对，未 live 复跑。
- 观察（2026-10-18 Windows live）：停留在会话页时，前端以每秒数次的频率轮询 `GET /api/sessions/<id>/model` 与 `GET /api/model-settings`（浏览器网络记录）。
