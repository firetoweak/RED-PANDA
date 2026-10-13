# 实时事件与连接

页面通过 `GET /api/events`（SSE）接收所有实时更新，连接 id 用于后续写操作。来源：`app.py` 的 `stream_events`、`redpanda/channels/web/hub.py`、`web/src/realtime/eventBridge.ts`、`runtimeSlice.ts`、`textDeltaBuffer.ts`。

## Sub-features

- `connect`：首个事件 `connected` 带 `connection_id`；空闲时发 `keep-alive` 注释。
- `stream`：`preview.started/delta/aborted`、`thinking.started/delta/finished/aborted`、`output_final`、`tool_progress`、`authorization_required`、`session_activity`、`conversation_status`、`context_usage`、`schedule_changed`、`session_failed`。
- `reconnect`：断线后界面显示 `正在连接…`，恢复后 `已连接`。
- `multi-tab`：多个标签页同时连接，都能看到同一会话的更新。
- `file-changes-refresh`：会话变更面板复用 `connected`（含重连）、`session_activity`、`tool_progress` 的 `settled` 状态，合并短时间内通知后重新查询；没有独立的变更 SSE 事件。

## How to get to it (user POV)

- 正常打开页面即建立；两个标签页打开同一会话即可观察多端同步。

## Driving it with the browser

- **连接**：`curl -N $REDPANDA_VERIFY_URL/api/events` → (b) 首事件为 `event: connected`，含 `connection_id`；保持连接可看到 keep-alive。
- **流式**：发一条消息时 (b) 依次出现 `preview.*`/`thinking.*`、`output_final`；`output_final` 文本与 `GET /api/sessions/<id>` 最终回复一致（无丢字、重复）。
- **多标签页**：A 页发消息，B 页同会话无刷新出现同样回复；侧栏状态同步。
  - (d) 用断开前的旧 `connection_id` POST 写接口 → 409 带 `detail`（失效连接按入口分类），不应 500。
- **变更同步**：A、B 页打开同一会话，B 页展开 `会话变更`；A 页让模型新建 `verify_refresh.txt`，写入一行 `hello`。工具完成后，B 页无手动刷新出现文件和新增行数，点击文件能看到新行号与正文；用 `GET /api/sessions/<id>/changes` 复核。工具失败也会触发刷新，不能按失败状态断言没有文件效果。重连后应重新查询；另一会话的文件操作也可能改变当前会话成果，所以刷新不只针对事件所属会话。细节见 [session-file-changes.md](./session-file-changes.md)。
- **断开释放**：关闭所有标签页后，(c) 后台运行中的回合不因断开而被误取消，或按设计取消，记录实际行为；服务日志无未处理异常。

## Gotchas

- 代理或浏览器扩展可能缓冲 SSE；用 `curl -N` 做基准。
- 外部编辑器直接修改文件不会产生这些 SSE 通知，首版用 `刷新文件变更` 更新；不把未监听外部编辑当成事件丢失。2026-10-13 新增的变更刷新步骤仅核对源码，未更新下方历史 live 状态。

## Live status
- 部分（2026-10-12，修复后）：流式通过；失效 connection_id 返回 409（隔离 API 通过）；断线重连未测
