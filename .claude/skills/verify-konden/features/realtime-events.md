# 实时事件与连接

页面通过 `GET /api/events`（SSE）接收所有实时更新，`connected` 事件里的 `connection_id` 用于后续写操作。来源：`konden/channels/web/app.py` 的 `stream_events` 与 `SSE_KEEPALIVE_SECONDS`、`konden/channels/web/hub.py`（`subscribe` 快照）、`konden/channels/web/channel.py`（`_require_connection`）、`konden/assistant/host/supervisor.py`（`release`）、`web/src/realtime/eventBridge.ts`、`runtimeSlice.ts`、`textDeltaBuffer.ts`、`AgentSidebar.tsx`、`Composer.tsx`。

## Sub-features

- `connect`：首个事件 `connected` 带 `connection_id`；紧接着服务端补发一份当前运行态快照（运行中会话的 `session_activity`=`running`、运行中工具的 `tool_progress`=`running`、进行中的 `preview.started`/`preview.delta` 与 `thinking.started`/`thinking.delta`）；空闲时约每 15 秒有 keep-alive 注释，应用输出 `: keep-alive`，框架也可能输出 `: ping`，不把注释当业务事件。
- `stream`：`preview.started/delta/aborted`、`thinking.started/delta/finished/aborted`、`output_final`、`tool_progress`、`authorization_required`、`session_activity`、`conversation_status`、`context_usage`、`schedule_changed`、`session_failed`、`group_changed`、`auto_authorize_changed`（全局开关值）。
- `reconnect`：断线期间侧栏底部显示 `正在连接…`，输入框禁用且占位文字为 `正在连接…`；恢复后连接图标（`role="status"`）的可访问名称为 `已连接`，不常显文字。
- `response-order`：同一会话的 GET 与操作响应交错时，正式投影保留较新的事实版本；旧连接或旧会话的选择确认不改变当前绑定。最终快照与同一输出的有序 SSE 交错，不产生重复回复或回退正文。
- `multi-tab`：多个标签页同时连接，都能看到同一会话的更新。
- `disconnect-release`：连接断开只释放该连接的会话选择，没有其他连接选择且空闲的 Worker 会被停掉；运行中的回合不会被取消。

## How to get to it (user POV)

- 正常打开页面即建立；两个标签页打开同一会话即可观察多端同步。

## Driving it with the browser

- **连接**：`curl -N $KONDEN_VERIFY_URL/api/events`。(b) 首事件为 `event: connected`，含 `connection_id`；空闲至少 16 秒，记录约 15 秒到达的 SSE 注释（`: keep-alive` 或 `: ping`），不固定只认一种。(a) 有会话正在运行时新连上的 curl 在 `connected` 之后立刻收到该会话的 `session_activity`（`running`）及进行中的 `preview.*`/`tool_progress` 快照。(c) 快照只描述当下，不重放历史的 `output_final`。(d) 见下方失效连接。
- **流式**：发一条消息，(b) 依次出现 `preview.*`/`thinking.*`、`output_final`；(a) `output_final` 文本与 `GET /api/sessions/<id>` 最终回复一致（无丢字、重复）。(c) 回合进行中刷新页面：新连接的快照接上进行中的预览，最终文本仍与 `GET` 一致。
- **多标签页**：A 页发消息，B 页同会话无刷新出现同样回复；侧栏状态同步。在一页切换全局自动放行，另一页无需刷新更新开关值。
  - (d) 写接口传断开前的旧 `connection_id` → 409，`detail` 为 `Web 连接已失效，请重新连接页面。`，不应 500。JSON 写接口传空 `connection_id` 会先被请求模型（`min_length=1`）拦下返回 422；另一条 409 文案 `Web 连接无效，请重新连接页面。` 只在未经该校验的入口出现（如 `POST .../attachments` 的 multipart 表单字段 `connection_id` 为空），可用它探测。
- **断线重连**：停掉服务（或断开网络）后，(b) 侧栏底部变 `正在连接…`，输入框禁用、占位 `正在连接…`；恢复后连接图标的可访问名称变为 `已连接`，输入框可用，(a) 页面内容与 `GET /api/sessions/<id>` 一致。
- **响应交错的页面验收**：在真实输出中刷新页面，切到另一位 Bot 或群后再返回。(a) 最终正文与 `GET` 一致，同一输出只显示一次。(b) 另一位 Bot 与群不出现原 Bot 的正文。(c) 各自草稿保留，切换不取消原执行。(d) 未在浏览器注入延迟时，不把普通切换记成故障窗口已实测；可控故障顺序使用下表的自动化入口。
- **断开释放**：让模型跑 `sleep 30 && echo DONE > done.txt`，运行中关闭所有标签页。(c) 回合不被取消：30 秒后 `done.txt` 出现，重新打开页面后该调用为成功、回合正常结束。(a) 会话空闲后，其 Worker 进程退出（进程查询见 crash-recovery）。(b) 服务日志无未处理异常。

**可复用的响应时序基线。** 修改会话投影缓存、连接／选择确认、Bot／群导航或流式展示时，按受影响的契约复跑对应场景。下面是验收方法，不是某次验证的通过记录；历史结论与未验证范围仍记在 `Gotchas`。

| 场景 | 可控触发 | 验收不变量 | 自动化入口 |
|---|---|---|---|
| 快照响应逆序 | 同时发起 GET 与发送请求，分别让旧 GET、旧操作响应最后返回 | 最终缓存保留较新版本及其回复，不随返回顺序回退 | [conversationTiming](../../../../web/tests/conversationTiming.test.tsx) 的「较新快照不被旧响应覆盖」 |
| 旧选择确认 | 选择请求尚未确认就断线重连，或切到另一位 Bot 后再完成原确认 | 旧确认不能重新绑定当前选择；不能因此重复选择当前 Bot | 同文件的「旧连接的选择确认晚到」及 Bot 切换用例 |
| Bot 请求跨导航返回 | Ada 的读取尚未结束就切到 Lin，分别让旧读取成功、失败，再完成旧选择确认 | 当前页面保留 Lin 的内容；不显示 Ada 的正文或旧错误；进入群后不再标记查看私聊 | 同文件的「切换 Bot 和群之后」 |
| 群请求跨导航返回 | 旧群仍在读取时切到新群并输入草稿，再让旧读取成功或失败 | 新群消息与草稿不变，旧群正文和错误不串入 | 同文件的「切换群后」 |
| 最终快照与流式交错 | 先让旧连接有未刷出的增量再断线；重连后把最终 HTTP 快照分别插在新流的开始、两段增量、最终结果之间及末尾 | 旧残片被清除；快照到达后，每个后续步骤都只呈现一份完整回复；最终没有悬挂预览 | [realtimeReconnect](../../../../web/tests/realtimeReconnect.test.ts) 的「重连后最终快照插在」 |

定位时可在仓库根运行：

```sh
npm --prefix web test -- --run tests/conversationTiming.test.tsx tests/realtimeReconnect.test.ts
```

Windows PowerShell 可使用 `npm.cmd`。这些用例也包含在默认 `npm test` 中；上述定向命令不替代提交前应做的完整检查。测试以实际 API 缓存、事件桥和页面为对象，预期回复及版本来自独立样例；HTTP 与 SSE 可以交错，同一 SSE 连接内仍保持发送顺序。业务契约变化时同步改判据与测试，不为保留旧用例增加兼容路径。

## Gotchas

- 代理或浏览器扩展可能缓冲 SSE；用 `curl -N` 做基准。
- 外部编辑器直接修改文件不会产生这些 SSE 通知，首版用 `刷新文件变更` 更新；不把未监听外部编辑当成事件丢失。
- 历史 live 状态：部分（2026-10-12，修复后）：流式通过；失效 connection_id 返回 409（隔离 API 通过）；断线重连未测。2026-10-18 按源码补充快照、keep-alive 间隔、断开释放与 409 文案，未重新 live 验证。
- 2026-10-18 Windows live：SSE 首事件 `connected`（0.0 s），空闲 15.0 s 后注释 `: ping`；陈旧 `connection_id` 调写接口 409 `Web 连接已失效，请重新连接页面。`。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：Doctor 首事件通过；A 标签发 MULTITAB_OK，B 同会话无刷新出现用户消息和最终回复。断线重连、保活时间与陈旧连接未重验。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：Doctor 首事件与双标签实时状态、全局自动授权同步通过；保活时间、断线重连和失效连接未重验。

- 2026-10-23 时序增量：Windows Chrome 1440×900、独立 home/端口、真实 DeepSeek。首行出现后刷新，最终 150 行编号完整且与只读 GET 正文一致，同一回复只呈现一次；切到另一 Bot 和群无串入正文，私聊与群草稿分别保留。旧响应成功/失败、旧连接确认与五种快照交错由自动化回归验证；未在浏览器注入延迟或断网，未重验多标签与 Linux。证据 `.tools/verification/frontend-timing/evidence/`。
