# 工具授权

写文件、执行命令等工具默认要用户批准；可按会话打开自动放行；某些控制请求要用户确认。来源：`ExecutionProcess.tsx`、`Composer.tsx`、`Conversation.tsx`、`app.py` 的 `/authorize`、`/auto-authorize`、`/control`、`redpanda/tools/builtin/exec_policy.py`。

## Sub-features

- `pending`：步骤显示 `待授权`，回合显示 `等待授权`，侧栏/SSE 发 `authorization_required`。
- `approve` / `reject`：批准后执行；拒绝后步骤显示 `已拒绝`。
- `auto-authorize`：输入框开关 `自动放行写文件等工具` → `POST .../auto-authorize`。
- `control-confirm`：控制请求显示 `等待确认`，`POST .../control` 带 `approved`。

## How to get to it (user POV)

- 在自动放行关闭的会话里让模型写文件：`请用 apply_patch 把 note.txt 改成 edited`。

## Driving it with the browser

- **拒绝**：出现 `待授权` 时点 `拒绝`（对话框标题 `等待授权`，按钮 `允许` / `拒绝`）。(a) `note.txt` 仍为 `verify`（逐字节）；`GET /api/sessions/<id>` 中该命令状态为拒绝。(b) authorize 2xx，步骤显示 `已拒绝`。(c) 无任何工作区文件 mtime 变化。(d) 对同一 `command_id` 再次 authorize → 应 4xx 或幂等无副作用，不能二次执行。
- **批准**：重发同请求后点 `允许`。(a) `note.txt` 为 `edited`。(b) 步骤 `完成`。
- **自动放行**：打开开关后再让模型写 `auto`。(a) 不出现 `待授权`，文件直接变为 `auto`；刷新页面后开关仍为开，`GET /api/sessions/<id>` 中 `session.auto_authorize` 为 true。(c) 其他会话的自动放行状态不变。(d) 关掉后同类请求重新出现 `待授权`。
- **授权超时/断线**：待授权时关闭页面再打开，(a) 请求仍在 `待授权`，不会被默认放行。

## Gotchas

- 只读命令（被 `exec_policy` 判为只读）不需要授权，也不进入文件回滚范围。
- 自动放行只覆盖「写文件等工具」；MCP/Skill/CLI 的 `propose_*` 安装类请求仍需确认，见 extensions。

## Live status
- 失败（2026-10-12，main@1a4790a）：正常批准/拒绝通过
- 已确认问题（证据路径相对 `/workspace/verify-run-20261012`）：
  - 无待授权项时拒绝/重复授权返回 500：`evidence/tool-authorization/results.json`
