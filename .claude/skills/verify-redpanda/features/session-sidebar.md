# 会话侧栏

左侧按工作区分组列出会话，支持新建、切换、改标题、归档、折叠分组，并显示每个会话的运行状态与相对时间。来源：`web/src/features/sessions/SessionSidebar.tsx`、`WorkspaceHeader.tsx`、`workspaces.ts`、`web/src/features/conversation/Conversation.tsx`、`app.py` 的 `/api/sessions`、`/select`、`/title`、`/archive`、`/api/session-titles`、`redpanda/assistant/host/supervisor.py` 的 `archive`。

## Sub-features

- `new-session`：侧栏顶部按钮 `新建会话`（无连接或没有工作区时不可点）→ 落到缺省工作区（最近有会话活动的工作区，否则最近创建的工作区）的草稿；各组标题上的 `在 <名称> 新建会话` 落到该工作区。草稿流程见 web-session-chat。
- `switch`：点会话行主体（可访问名称为会话标题）→ 地址变为 `/sessions/<id>`。`POST /api/sessions/{id}/select` 不是侧栏发的，而是进入 `/sessions/:sessionId` 后由 `Conversation.tsx` 在本连接当前所属会话不是该会话时发出。
- `rename`：点 `重命名 <标题>`（悬停提示 `重命名`），或双击标题 → 输入框 `会话标题` → 回车或失焦保存（`POST .../title`），`Esc` 取消。去掉首尾空白后为空或与原标题相同 → 不发请求，恢复原标题。
- `archive`：点 `归档 <标题>`（悬停提示 `归档`）→ `POST .../archive`。后端先暂停会话、取消当前回合、收回其子 Agent 并丢弃子 Agent 工作副本，再标记归档并停止该会话的持续进程；归档从不因会话正在运行而被拒绝。没有取消归档入口。
- `status`：每行左侧活动点（`span.activity-dot`，aria-label 与悬停提示相同）取值 `需要操作`（会话有未消除的失败、待授权或待确认请求）、`运行中`、`等待子 Agent`、`执行已结束，有新结果`（有未读结果）、`空闲`；侧栏底部连接状态文字 `已连接` / `正在连接…`。
- `relative-time`：行尾显示 `updated_at` 的相对时间：`刚刚`、`<n>m`、`<n>h`、`<n>d`；草稿行无时间。
- `group-collapse`：点工作区标题（`.workspace-toggle`，带 `aria-expanded`）折叠/展开该组；折叠状态存于 `localStorage` 的 `redpanda.collapsedWorkspaces`；在折叠组里新建会话会自动展开该组。
- `group-more`：每组最多先显示 5 个会话（草稿行另算），其余收在 `更多 · N` 后，点击后展开全部（不持久）。
- `auto-title`：首轮后自动生成标题（`GET /api/session-titles`）。

## How to get to it (user POV)

- 侧栏顶部 `新建会话`；每个会话行悬停出现 `重命名`、`归档` 图标；双击标题也可改名；点工作区名折叠分组。

## Driving it with the browser

- **改标题**：点 `重命名 <标题>`，改为 `验证标题A` 后回车。
  - (a) `GET /api/sessions` 中该会话 `title` 为 `验证标题A`，`GET /api/session-titles` 同样；刷新页面后仍是。
  - (b) `POST .../title` 2xx，响应 `{"title": "验证标题A"}`；侧栏行与 `重命名 验证标题A` 名称随之更新。
  - (c) 会话消息与 `note.txt` 不变；其他会话标题不变。
  - (d) 输入空串或全空格回车、或不改内容回车 → 网络面板无 `POST .../title`，标题恢复原值；按 `Esc` 同样恢复且无请求。直接 `curl` 发空标题（探测用）→ 422，标题不变。
- **归档**：先归档一个空闲会话，再归档一个 `运行中` 的会话。
  - (a) 该会话从侧栏与 `GET /api/sessions` 消失；`GET /api/sessions/<id>` 仍返回 200 且有历史；`runtime_sessions/` 下目录不被删除。
  - (b) `POST .../archive` 返回 200、响应 `{}`；正在看该会话时页面跳到所属工作区（随后落到该工作区的草稿）。
  - (c) 工作区文件不变；其他会话不受影响。
  - (d) 归档 `运行中` 的会话 → 不被拒绝，同样 200；之后 SSE 上不再有该会话新的回合进展，该会话启动的持续进程已被停止（对应进程不再存在），子 Agent 被收回。归档不存在的会话 id → 404。
- **切换**：两个会话来回切。(a) 每次显示的消息与 `GET /api/sessions/<id>` 一致。(b) 进入未持有的会话时出现一次 `POST .../select` 2xx；再切回当前已持有的会话不重复发。(d) `POST /api/sessions/不存在/select` → 404。
- **状态徽标**：发一条会触发长命令的消息。(b) 运行中该行活动点名称为 `运行中`；结束后若未打开该会话为 `执行已结束，有新结果`，打开后回到 `空闲`；有待授权、待确认请求或回合失败时为 `需要操作`。(a) 对照 `GET /api/sessions` 的 `activity`、`needs_attention`、`has_active_subagents`。
- **折叠与更多**：(a) 折叠后 `localStorage.redpanda.collapsedWorkspaces` 含该工作区 id 为 `true`，刷新后仍折叠。(b) 组内超过 5 个会话时出现 `更多 · N`，N 等于未显示数。(c) 折叠、展开不发任何 API 请求。(d) 手动把该 `localStorage` 值改成非 JSON 后刷新 → 所有组按展开显示，不报错。

## Gotchas

- 自动标题依赖模型；`live_model_ready` 为 false 时跳过 `auto-title`。
- 源码没有「取消归档」入口；归档后只能经 URL 直达只读查看，这是当前设计的已知缺口。
- 按名称定位时注意：行主体名称是完整标题，`重命名 <标题>` / `归档 <标题>` 才是按钮；`重命名`、`归档` 只是悬停提示文字。
- 草稿行标题为 `new agent`，在首条消息前不在 `GET /api/sessions` 里。
- 历史 live 记录：部分（2026-10-12，main@1a4790a）：归档不可撤销；证据 `evidence/session-sidebar/results.json`（相对 `/workspace/verify-run-20261012`）。2026-10-18 的修订只做源码核对，未 live 复跑。
