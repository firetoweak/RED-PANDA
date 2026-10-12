# 实时事件与连接

页面通过 `GET /api/events`（SSE）接收所有实时更新，连接 id 用于后续写操作。来源：`app.py` 的 `stream_events`、`redpanda/channels/web/hub.py`、`web/src/realtime/eventBridge.ts`、`runtimeSlice.ts`、`textDeltaBuffer.ts`。

## Sub-features

- `connect`：首个事件 `connected` 带 `connection_id`；空闲时发 `keep-alive` 注释。
- `stream`：`preview.started/delta/aborted`、`thinking.started/delta/finished/aborted`、`output_final`、`tool_progress`、`authorization_required`、`session_activity`、`conversation_status`、`context_usage`、`schedule_changed`、`session_failed`。
- `reconnect`：断线后界面显示 `正在连接…`，恢复后 `已连接`。
- `multi-tab`：多个标签页同时连接，都能看到同一会话的更新。

## How to get to it (user POV)

- 正常打开页面即建立；两个标签页打开同一会话即可观察多端同步。

## Driving it with the browser

- **连接**：`curl -N $REDPANDA_VERIFY_URL/api/events` → (b) 首事件为 `event: connected`，含 `connection_id`；保持连接可看到 keep-alive。
- **流式**：发一条消息时 (b) 依次出现 `preview.*`/`thinking.*`、`output_final`；`output_final` 文本与 `GET /api/sessions/<id>` 最终回复一致（无丢字、重复）。
- **多标签页**：A 页发消息，B 页同会话无刷新出现同样回复；侧栏状态同步。
- **断线**：杀掉服务再重启（同一 home）。(b) 页面显示 `正在连接…` 后恢复 `已连接`。(a) 重连后历史与 `GET` 一致。(d) 用断开前的旧 `connection_id` POST 写接口 → 应返回 4xx 带 `detail`，不应 500（见报告疑似问题：`Web connection is not active` 是 `ValueError`，`app.py` 未注册 `ValueError` 处理器）。
- **断开释放**：关闭所有标签页后，(c) 后台运行中的回合不因断开而被误取消，或按设计取消，记录实际行为；服务日志无未处理异常。

## Gotchas

- 代理或浏览器扩展可能缓冲 SSE；用 `curl -N` 做基准。
