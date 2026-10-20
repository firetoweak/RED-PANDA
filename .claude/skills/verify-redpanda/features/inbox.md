# 跨会话收件箱

侧栏按钮 `待处理` 打开 `/inbox`；有事项时显示独立数字徽标，无障碍名称为 `待处理 · N`。页面汇总各 Agent 的命令授权、控制提案、交流超限、暂停未处理消息和无人查看例行任务询问。点条目标题进入 `/sessions/:sessionId`。允许、拒绝、取消、确认走原来的 `POST .../authorize` 和 `POST .../control`，不另开批准接口。会话输入框上方的待处理条仍然保留。来源：`InboxPage.tsx`、`AgentSidebar.tsx`、`GET /api/inbox`、`redpanda/assistant/conversations.py` 的 `list_inbox`。

## Sub-features

- `empty`：没有待决定事项时，侧栏按钮文字是 `待处理`，页面文字是 `没有待处理事项`。
- `authorization`：一条命令授权显示 Agent 名字、工具名和参数摘要；`查看 <工具名> 的参数` 展开完整参数 JSON，按钮 `允许` / `拒绝`。
- `control`：一条控制提案显示摘要和 `风险：…`，按钮 `取消` / `确认`。
- `open`：点标题进入该会话。
- `limit`：跳数或源头总量达到上限时出现该 Agent 的上报，没有批准按钮；人在该会话再发消息后消失。
- `paused`：已暂停且仍有未处理消息的 Agent 有一条提示，按钮 `继续` 走会话的继续。
- `absence`：无人查看满时间门槛，且未读汇报与例行任务触发计数都到门槛时，问 `继续` / `停下`；超时未回应后变为 `恢复`，只恢复这次暂停的例行任务。

## How to get to it (user POV)

- 侧栏点 `待处理`。五类事项均计入数量徽标。

## Driving it with the browser

- **空收件箱**：五类事项都不存在时打开 `/inbox`。
  - (a) `GET /api/inbox` 为 `[]`。
  - (b) 页面显示 `没有待处理事项`；侧栏按钮为 `待处理`。
  - (c) Agent 名册与各会话的待处理条不因打开收件箱而改变。
  - (d) 未连接时按钮仍可进入页面；页面上的 `允许` / `拒绝` / `取消` / `确认` 不可用。
- **命令授权**：在自动放行关闭的会话里让模型写文件，停在待授权，再打开收件箱。
  - (a) `GET /api/inbox` 含 `kind` 为 `authorization` 的一项，带 `session_id`、`command_id`、`name`、`arguments`；没有 `hop`。`note.txt` 仍是 `verify`。
  - (b) 侧栏按钮的无障碍名称为 `待处理 · N`；该行标题链到 `/sessions/<id>`；点 `查看 <工具名> 的参数` 展开 JSON，再点收起；点 `允许` 后 `POST .../commands/{command_id}/authorize` 2xx，该项从收件箱消失，`note.txt` 变为请求的内容。
  - (c) 会话内待处理条与收件箱是同一批事项；批准一处，另一处也消失。子会话与 TUI 会话不出现。
  - (d) 点 `拒绝` 后文件保持 `verify`，该项消失；对同一 `command_id` 再批准 → 409。
- **控制提案**：让模型发起一个控制请求后打开收件箱。
  - (a) `GET /api/inbox` 含 `kind` 为 `control` 的一项，带 `request_id`、`summary`、`risk`。
  - (b) 行内有摘要、`风险：…`、`取消` 和 `确认`；点 `确认` 后 `POST .../control` 2xx，该项消失。
  - (c) 这一条是控制提案，不把跳数超限混进同一行。
  - (d) 点 `取消` 后该项消失，对应控制未执行。

## Gotchas

- 还没有用户消息的 Agent，只要已有待授权或控制提案，也会出现在收件箱。
- 跳数超限、暂停未处理消息和无人查看询问与命令授权、控制提案列在同一页。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：真实 write_file 授权项、独立数字徽标、完整参数展开、允许后空收件箱通过；已暂停 Ada 收到私信后出现 paused 项，点继续后处理。交流上限与无人查看门槛未覆盖。
