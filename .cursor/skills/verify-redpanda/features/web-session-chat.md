# Web 会话与聊天

从空首页新建工作区，落到草稿会话，在配置了模型时发一条消息并看到回复。入口 `web_chat.py`，路由见 `web/src/app/App.tsx`，接口见 `redpanda/channels/web/app.py`。

## Sub-features

- `home-empty`：没有工作区时 `/` 由 `DraftRedirect`（`web/src/features/sessions/DraftRedirect.tsx`）显示 `正在连接…`/`正在打开会话…`，然后引导建工作区。
- `draft-session`：`/workspaces/:workspaceId` 打开草稿会话，首条消息发出时才 `POST /api/sessions` 建会话（`web/src/features/sessions/bindDraft.ts`）。
- `send-message`：在 `消息` 输入框（占位 `输入消息，Enter 发送，可拖入文件或粘贴图片`）回车或点 `发送`，走 `POST /api/sessions/{id}/inputs`。
- `reply-render`：回复以 Markdown 渲染（`MarkdownMessage.tsx`），可 `复制回复`；思考内容显示为 `思考中`（`ThinkingBlock.tsx`）。
- `spa-fallback`：任何未知路径都回 `index.html`（`app.py` 的 `/{path:path}`）。

## How to get to it (user POV)

- 打开 `$REDPANDA_VERIFY_URL/`，没有工作区时点侧栏 `新建工作区` 建一个指向 `$SCRATCH_WORKSPACE` 的工作区（见 workspace-management）。
- 自动进入该工作区的草稿会话，在底部输入框打字发送。

## Driving it with the browser

- **建会话**：发出首条消息 `请只回复 PING_OK`。
  - (a) `curl $REDPANDA_VERIFY_URL/api/sessions` 多出一个会话，`workspace_id` 等于刚建的工作区；`$REDPANDA_HOME/runtime_sessions/` 下多出该会话目录。地址栏变为 `/sessions/<id>`。
  - (b) `POST /api/sessions` 返回 201，`POST .../inputs` 返回 2xx；SSE 上有该会话的事件流（见 realtime-events）。
  - (c) `$SCRATCH_WORKSPACE/note.txt` 仍是 `verify`；`config.json`、`connections.json` 的 mtime 不变。
  - (d) 错误路径：直接 `curl -X POST .../inputs` 带一个不存在或已断开的 `connection_id` → 不应是 500，应是 4xx 且有 `detail`（见 Gotchas 疑似问题）。打开 `/sessions/session-不存在` → 页面显示 `这个会话不存在。请从左侧新建或选择会话。`，`GET /api/sessions/session-不存在` 返回 404。
- **回复**：(a) `GET /api/sessions/<id>` 里有用户消息与助手回复，回复正文含 `PING_OK`。(b) 界面出现助手气泡，`复制回复` 可点。(c) 会话数不因刷新页面而增加。(d) 模型 key 错误时，界面出现失败提示（`消息发送失败，请确认后端连接后重试。` 或回合失败卡片），journal 里有失败事实而不是空回复。
- **SPA 回落**：`curl -i $REDPANDA_VERIFY_URL/whatever/deep` 返回 200 且是 `index.html`；`curl -i $REDPANDA_VERIFY_URL/api/不存在` 应返回 404 JSON，而不是 `index.html`（核对路由顺序）。

## Gotchas

- 草稿会话在发出首条消息前不在 `GET /api/sessions` 里，这是设计不是丢失。
- `live_model_ready` 为 false 时只验到建会话为止。
- `--workspace` 启动会预先登记工作区，跳过空首页路径；验证时不要用。
- `web/dist` 不存在时 `/` 不会有页面，只有 API。
