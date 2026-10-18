# 工具授权

同一批待授权调用和控制请求也汇总在侧栏 `待处理` 打开的 `/inbox`（见 [inbox.md](./inbox.md)）；会话内的待处理条仍然保留。写文件工具 `write_file`、`apply_patch`、`replace_all`，以及 `execute_command`、`start_process` 每次都要用户批准；命令不按文本放行，`git status` 这类只读命令也要批准。Agent 委派的子 Agent 在自己的 worktree 里写文件不需要授权（`redpanda/assistant/builtin_tools.py`），`merge_subagent` 把子合进 Agent 自己的分支，也不需要授权。可按会话打开自动放行；模型发起的控制请求另需确认。待授权调用与控制请求都列在输入框上方的待处理条里，没有弹窗。来源：`ExecutionProcess.tsx`、`PendingActions.tsx`、`Composer.tsx`、`Conversation.tsx`、`app.py` 的 `/authorize`、`/auto-authorize`、`/control`、`redpanda/assistant/sessions.py`、`redpanda/assistant/host/supervisor.py`、`redpanda/tools/builtin/command_execution.py`。

## Sub-features

- `pending`：需要授权的工具调用在步骤卡片上显示徽标 `待授权`，卡片内有 `允许`/`拒绝`；同时输入框上方出现区域 `待处理`（标题 `待处理 · N 项`），每项为 `<工具名> · <参数摘要>`，按钮 `允许 <工具名>`/`拒绝 <工具名>`（可见文字 `允许`/`拒绝`），点 `查看 <工具名> 的参数` 展开参数 JSON。待处理条只读服务端 `session.pending_authorization_commands`，与步骤卡片同源；SSE 发 `authorization_required`；侧栏该会话活动点为 `需要操作`。
- `approve` / `reject`：`POST .../commands/{command_id}/authorize`（`approved` 为 true/false）。批准后执行，徽标变 `运行中` 再到 `完成`；拒绝后徽标 `已拒绝`，不执行。
- `auto-authorize`：输入框下方开关 `自动放行写文件等工具` → `POST .../auto-authorize`（`enabled`）。按会话持久化在 `$REDPANDA_HOME/runtime_sessions/auto_authorize.json`，重启后仍生效；打开的瞬间会把该会话当前所有待授权调用一并批准。
- `control-confirm`：模型发起的控制请求作为一项出现在待处理条里，显示摘要与 `风险：…`，按钮 `取消`/`确认`；对话上下文保持可见。读 `session.control_approval`；`POST .../control` 带 `request_id`、`approved`。

## How to get to it (user POV)

- 在自动放行关闭的会话里让模型写文件：`请用 apply_patch 把 note.txt 改成 edited`。

## Driving it with the browser

- **待授权**：发送上面的请求。
  - (a) `GET /api/sessions/<id>` 中该调用处于待授权；`GET /api/inbox` 含这条授权；`note.txt` 仍是 `verify`。
  - (b) SSE 收到 `authorization_required`（带 `command_id`）；步骤徽标 `待授权`；区域 `待处理` 显示 `待处理 · 1 项`、`apply_patch · note.txt`；页面没有任何 `dialog`。
  - (c) 在批准前工作区文件不变，不出现任何写入。
  - (d) 关闭页面再打开 → 待处理条与步骤卡片仍在，不会被默认放行。
- **拒绝**：点 `拒绝 apply_patch`（或步骤卡片上的 `拒绝`）。
  - (a) `note.txt` 仍是 `verify`；journal 里该调用为拒绝。
  - (b) `authorize` 返回 2xx；步骤徽标 `已拒绝`；待处理条消失；模型收到拒绝后继续本轮。
  - (c) 其他步骤与会话不受影响。
  - (d) 对同一 `command_id` 再次 authorize（批准或拒绝）→ 409，`detail` 为 `该操作已处理或当前不可授权：<command_id>`，无二次执行；不存在的 `command_id` → 404 `授权目标不存在：<command_id>`。
- **批准**：重发同请求后点 `允许 apply_patch`。
  - (a) `note.txt` 为 `edited`。
  - (b) `authorize` 返回 2xx；步骤徽标经 `运行中` 到 `完成`，待处理条消失。
  - (c) 只执行一次，文件只被改一次。
  - (d) 批准后再对同一 `command_id` 拒绝 → 409，文件保持 `edited`。
- **自动放行**：先让模型写一个文件停在 `待授权`，再打开开关；然后让模型写 `auto`。
  - (a) 打开开关后原待授权调用立即被批准并执行，待处理条随服务端状态消失（2026-10-18 实测 0.5 s 内）；之后不再出现 `待授权`，文件直接变为 `auto`；`GET /api/sessions/<id>` 中 `session.auto_authorize` 为 true；`runtime_sessions/auto_authorize.json` 里该会话为 true；重启服务后刷新页面开关仍为开。
  - (b) `POST .../auto-authorize` 2xx。
  - (c) 其他会话的自动放行状态不变；`execute_command` 同样被自动放行（它也是授权工具）。
  - (d) 关掉后同类请求重新出现 `待授权`。
- **控制确认**：触发一个模型控制请求（如 extensions 中的 `propose_*` 安装类请求）。
  - (a) `GET /api/sessions/<id>` 中 `session.control_approval` 非空，含摘要与风险。
  - (b) 待处理条出现该项（摘要、`风险：…`、`取消`/`确认`），上方对话仍可阅读；点 `确认` 或 `取消` 后 `POST .../control` 2xx，该项消失。
  - (c) `取消` 后被提议的变更不生效；重复提交与已作决定相同的选择返回原结果，不重复执行。
  - (d) 对已拒绝的请求再 `approved: true`、对已批准/已执行的请求再 `approved: false`、或用不存在的 `request_id` → 应是 409 且有 `detail`（当前源码不满足，见 Gotchas）。

## Gotchas

- 所有 `execute_command` 调用都需授权，包括只读命令；自动放行关闭时，验证中要跑的每条命令都会停在 `待授权`。
- 没有单独的「文件管理」工具；`file_manage.py` 只注册 `write_file`。
- 自动放行只覆盖「写文件等工具」这类工具授权；控制请求（如 MCP/Skill/CLI 的 `propose_*` 安装类）不受它影响，仍需在待处理条确认，见 extensions。
- 待处理条与步骤卡片都有 `允许`/`拒绝`：待处理条按钮的无障碍名称带工具名（`允许 apply_patch`），按名称点击时用它避免歧义。
- 授权入口的已知错误有明确状态码：失效连接 409、授权目标不存在 404、重复授权/拒绝 409；未知内部异常仍原样暴露，不做全局兜底。
- 已知产品缺口（2026-10-18 源码核对）：`/control` 的冲突（`ControlDecisionConflict`，继承 `ValueError`，`redpanda/assistant/control.py`）以及「当前没有待确认的控制操作」（`NoPendingControlApproval`）在 `app.py` 都没有异常处理器，预计返回 500 而不是 409。判据保持 409 的预期。
- 2026-10-18 改写：命令放行名单删除，命令一律授权，`merge_subagent` 不再需要授权。未 live 复跑。
- 历史 live 记录：2026-10-16（main@e191eab，仅 API，界面未验证）：`write_file` 待授权 → 拒绝 200、文件未建、重复拒绝 409、无目标 404；批准后落盘；`rm -f` 进入待授权；`touch` 不需授权。部分（2026-10-12，修复后）：重复授权/无目标返回 500 已修（目标不存在 404、重复授权/拒绝 409；自动化 + 隔离 API 通过）。2026-10-18 Windows live（弹窗版本）发现：待授权时打开自动放行后 `等待授权` 弹窗残留——随弹窗改为待处理条已消除。同日 Windows live（deepseek-flash，待处理条版本）：待授权 → 拒绝 / 允许、待授权时打开自动放行、控制确认取消与确认均通过；页面无弹窗。
