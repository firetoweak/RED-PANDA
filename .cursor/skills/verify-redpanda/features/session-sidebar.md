# 会话侧栏

左侧列出所有会话，支持切换、改标题、归档，并显示运行状态。来源：`web/src/features/sessions/SessionSidebar.tsx`、`app.py` 的 `/api/sessions`、`/select`、`/title`、`/archive`、`/api/session-titles`。

## Sub-features

- `switch`：点会话（aria-label 为标题）→ `POST /api/sessions/{id}/select`，地址变为 `/sessions/<id>`。
- `rename`：`重命名 <标题>` → 输入框 `会话标题` → 回车，`POST .../title`。
- `archive`：`归档 <标题>` → `POST .../archive`。
- `status`：活动徽标 `空闲` / `运行中` / `等待子 Agent`，连接徽标 `已连接` / `正在连接…`。
- `auto-title`：首轮后自动生成标题（`GET /api/session-titles`）。

## How to get to it (user POV)

- 侧栏里每个会话行悬停出现 `重命名`、`归档` 操作。

## Driving it with the browser

- **改标题**：改为 `验证标题A`。(a) `GET /api/sessions` 中该会话 `title` 为 `验证标题A`，刷新页面后仍是。(b) `POST .../title` 2xx。(c) 会话消息与 `note.txt` 不变；其他会话标题不变。(d) 输入空串或全空格回车 → 不应保存为空标题（应保留原标题或报错），记下实际行为。
- **归档**：(a) 该会话从侧栏与 `GET /api/sessions` 列表消失（或带归档标记），但 `GET /api/sessions/<id>` 仍能读到历史，`runtime_sessions/` 下目录不被删除。(b) 2xx。(c) 工作区文件不变。(d) 归档一个正在 `运行中` 的会话 → 记录是被拒绝还是先停止；不得留下仍在跑的后台回合（再看 SSE 无该会话新事件）。
- **切换**：两个会话来回切，(a) 每次显示的消息与 `GET /api/sessions/<id>` 一致。(d) `POST /api/sessions/不存在/select` → 404。
- **状态徽标**：发一条会触发长命令的消息，运行中时该行显示 `运行中`，结束后回到 `空闲`。

## Gotchas

- 自动标题依赖模型；`live_model_ready` 为 false 时跳过 `auto-title`。
- 源码没有「取消归档」入口；归档后能否找回只能经 URL 直达，记为观察项。

## Live status
- 部分（2026-10-12，main@1a4790a）：归档不可撤销
- 已确认问题（证据路径相对 `/workspace/verify-run-20261012`）：
  - 归档后无法取消归档（功能缺口）：`evidence/session-sidebar/results.json`
