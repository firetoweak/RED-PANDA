# 实时事件与连接

页面通过 `GET /api/events`（SSE）接收所有实时更新，`connected` 事件里的 `connection_id` 用于后续写操作。来源：`redpanda/channels/web/app.py` 的 `stream_events` 与 `SSE_KEEPALIVE_SECONDS`、`redpanda/channels/web/hub.py`（`subscribe` 快照）、`redpanda/channels/web/channel.py`（`_require_connection`）、`redpanda/assistant/host/supervisor.py`（`release`）、`web/src/realtime/eventBridge.ts`、`runtimeSlice.ts`、`textDeltaBuffer.ts`、`AgentSidebar.tsx`、`Composer.tsx`。

## Sub-features

- `connect`：首个事件 `connected` 带 `connection_id`；紧接着服务端补发一份当前运行态快照（运行中会话的 `session_activity`=`running`、运行中工具的 `tool_progress`=`running`、进行中的 `preview.started`/`preview.delta` 与 `thinking.started`/`thinking.delta`）；空闲时约每 15 秒有 keep-alive 注释，应用输出 `: keep-alive`，框架也可能输出 `: ping`，不把注释当业务事件。
- `stream`：`preview.started/delta/aborted`、`thinking.started/delta/finished/aborted`、`output_final`、`tool_progress`、`authorization_required`、`session_activity`、`conversation_status`、`context_usage`、`schedule_changed`、`session_failed`、`group_changed`、`auto_authorize_changed`（全局开关值）。
- `reconnect`：断线期间侧栏底部显示 `正在连接…`，输入框禁用且占位文字为 `正在连接…`；恢复后侧栏显示 `已连接`。
- `response-order`：同一会话的 GET 与操作响应交错时，正式投影保留较新的事实版本；旧连接或旧会话的选择确认不改变当前绑定。最终快照与同一输出的有序 SSE 交错，不产生重复回复或回退正文。
- `multi-tab`：多个标签页同时连接，都能看到同一会话的更新。
- `disconnect-release`：连接断开只释放该连接的会话选择，没有其他连接选择且空闲的 Worker 会被停掉；运行中的回合不会被取消。
- `file-changes-refresh`：`文件改动` 复用 `connected`（含重连）、`session_activity`、`tool_progress` 的 `settled` 状态，合并短时间内通知后重新查询；没有独立的变更 SSE 事件。

## How to get to it (user POV)

- 正常打开页面即建立；两个标签页打开同一会话即可观察多端同步。

## Driving it with the browser

- **连接**：`curl -N $REDPANDA_VERIFY_URL/api/events`。(b) 首事件为 `event: connected`，含 `connection_id`；空闲至少 16 秒，记录约 15 秒到达的 SSE 注释（`: keep-alive` 或 `: ping`），不固定只认一种。(a) 有会话正在运行时新连上的 curl 在 `connected` 之后立刻收到该会话的 `session_activity`（`running`）及进行中的 `preview.*`/`tool_progress` 快照。(c) 快照只描述当下，不重放历史的 `output_final`。(d) 见下方失效连接。
- **流式**：发一条消息，(b) 依次出现 `preview.*`/`thinking.*`、`output_final`；(a) `output_final` 文本与 `GET /api/sessions/<id>` 最终回复一致（无丢字、重复）。(c) 回合进行中刷新页面：新连接的快照接上进行中的预览，最终文本仍与 `GET` 一致。
- **多标签页**：A 页发消息，B 页同会话无刷新出现同样回复；侧栏状态同步。在一页切换全局自动放行，另一页无需刷新更新开关值。
  - (d) 写接口传断开前的旧 `connection_id` → 409，`detail` 为 `Web 连接已失效，请重新连接页面。`，不应 500。JSON 写接口传空 `connection_id` 会先被请求模型（`min_length=1`）拦下返回 422；另一条 409 文案 `Web 连接无效，请重新连接页面。` 只在未经该校验的入口出现（如 `POST .../attachments` 的 multipart 表单字段 `connection_id` 为空），可用它探测。
- **断线重连**：停掉服务（或断开网络）后，(b) 侧栏底部变 `正在连接…`，输入框禁用、占位 `正在连接…`；恢复后变 `已连接`，输入框可用，(a) 页面内容与 `GET /api/sessions/<id>` 一致。
- **响应交错**：在真实输出中刷新页面，切到另一位 Bot 或群后再返回。(a) 最终正文与 `GET` 一致，同一输出只显示一次。(b) 另一位 Bot 与群不出现原 Bot 的正文。(c) 各自草稿保留，切换不取消原执行。(d) 慢 GET 晚于较新操作响应、旧选择确认晚于切换或重连，以及旧请求失败的确定性顺序，由 `web/tests/conversationTiming.test.tsx` 驱动真实 API 缓存与页面；最终快照插入有序流的五种位置由 `realtimeReconnect.test.ts` 覆盖。未在浏览器注入这些延迟时，不把普通切换记成故障窗口已实测。
- **变更同步**：A、B 页打开同一会话，B 页打开 `文件改动`；A 页让模型新建 `verify_refresh.txt`，写入一行 `hello`。工具完成后，B 页无手动刷新出现文件和新增行数，点击文件能看到新行号与正文；用 `GET /api/sessions/<id>/changes?project=<项目名>` 复核。工具失败也会触发刷新，不能按失败状态断言没有文件效果。重连后应重新查询；另一会话的文件操作也可能改变当前会话成果，所以刷新不只针对事件所属会话。细节见 [session-file-changes.md](./session-file-changes.md)。
- **断开释放**：让模型跑 `sleep 30 && echo DONE > done.txt`，运行中关闭所有标签页。(c) 回合不被取消：30 秒后 `done.txt` 出现，重新打开页面后该调用为成功、回合正常结束。(a) 会话空闲后，其 Worker 进程退出（进程查询见 crash-recovery）。(b) 服务日志无未处理异常。

## Gotchas

- 代理或浏览器扩展可能缓冲 SSE；用 `curl -N` 做基准。
- 外部编辑器直接修改文件不会产生这些 SSE 通知，首版用 `刷新文件变更` 更新；不把未监听外部编辑当成事件丢失。
- 历史 live 状态：部分（2026-10-12，修复后）：流式通过；失效 connection_id 返回 409（隔离 API 通过）；断线重连未测。2026-10-18 按源码补充快照、keep-alive 间隔、断开释放与 409 文案，未重新 live 验证。
- 2026-10-18 Windows live：SSE 首事件 `connected`（0.0 s），空闲 15.0 s 后注释 `: ping`；陈旧 `connection_id` 调写接口 409 `Web 连接已失效，请重新连接页面。`。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：Doctor 首事件通过；A 标签发 MULTITAB_OK，B 同会话无刷新出现用户消息和最终回复。断线重连、保活时间与陈旧连接未重验。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：Doctor 首事件与双标签实时状态、全局自动授权同步通过；保活时间、断线重连和失效连接未重验。

- 2026-10-23 时序增量：Windows Chrome 1440×900、独立 home/端口、真实 DeepSeek。首行出现后刷新，最终 150 行编号完整且与只读 GET 正文一致，同一回复只呈现一次；切到另一 Bot 和群无串入正文，私聊与群草稿分别保留。旧响应成功/失败、旧连接确认与五种快照交错由自动化回归验证；未在浏览器注入延迟或断网，未重验多标签与 Linux。证据 `.tools/verification/frontend-timing/evidence/`。
