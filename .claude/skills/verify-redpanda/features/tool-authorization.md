# 工具授权

写文件工具 `write_file`、`apply_patch`、`replace_all`，以及 `start_process`、`merge_subagent` 默认要用户批准；`execute_command` 只在 `command_requires_authorization`（`redpanda/tools/builtin/exec_policy.py`）判为需要时批准：命令文本命中危险前缀（`rm `、`del `、`git push -f `、`git reset --hard `、`git clean -f ` 等），或 `file_access=host` 且 `workspace_effect` 为 `may_write`（缺省即 `may_write`）；普通命令（如 `touch`）直接执行。子 Agent 隔离副本里三个写文件工具不需要授权（`redpanda/assistant/builtin_tools.py`）。可按会话打开自动放行；模型发起的控制请求另有确认弹窗。来源：`ExecutionProcess.tsx`、`Composer.tsx`、`Conversation.tsx`、`app.py` 的 `/authorize`、`/auto-authorize`、`/control`、`redpanda/assistant/sessions.py`、`redpanda/assistant/host/supervisor.py`、`redpanda/tools/builtin/command_execution.py`、`exec_policy.py`。

## Sub-features

- `pending`：需要授权的工具调用在步骤卡片上显示徽标 `待授权`，卡片内有 `允许`/`拒绝`；同时弹出标题为 `等待授权` 的弹窗（显示工具名与参数 JSON，按钮 `拒绝`/`允许`）；SSE 发 `authorization_required`；侧栏该会话活动点为 `需要操作`。
- `approve` / `reject`：`POST .../commands/{command_id}/authorize`（`approved` 为 true/false）。批准后执行，徽标变 `运行中` 再到 `完成`；拒绝后徽标 `已拒绝`，不执行。
- `auto-authorize`：输入框下方开关 `自动放行写文件等工具` → `POST .../auto-authorize`（`enabled`）。按会话持久化在 `$REDPANDA_HOME/runtime_sessions/auto_authorize.json`，重启后仍生效；打开的瞬间会把该会话当前所有待授权调用一并批准。
- `control-confirm`：模型发起的控制请求弹出标题为 `等待确认` 的弹窗，显示摘要与 `风险：…`，按钮 `取消`/`确认`；无关闭按钮，`Esc` 与点外部都不能关闭。`POST .../control` 带 `request_id`、`approved`。

## How to get to it (user POV)

- 在自动放行关闭的会话里让模型写文件：`请用 apply_patch 把 note.txt 改成 edited`。

## Driving it with the browser

- **待授权**：发送上面的请求。
  - (a) `GET /api/sessions/<id>` 中该调用处于待授权；`GET /api/sessions` 中该会话 `needs_attention` 为 true；`note.txt` 仍是 `verify`。
  - (b) SSE 收到 `authorization_required`（带 `command_id`）；步骤徽标 `待授权`，弹出 `等待授权` 弹窗，显示 `apply_patch` 与参数。
  - (c) 在批准前工作区文件不变，不出现任何写入。
  - (d) 关闭页面再打开 → 仍在 `待授权`，不会被默认放行；按 `Esc` 或右上角关闭 `等待授权` 弹窗只是收起弹窗，步骤卡片上的 `允许`/`拒绝` 仍在，调用仍待授权。
- **拒绝**：点 `拒绝`。
  - (a) `note.txt` 仍是 `verify`；journal 里该调用为拒绝。
  - (b) `authorize` 返回 2xx；步骤徽标 `已拒绝`；模型收到拒绝后继续本轮。
  - (c) 其他步骤与会话不受影响。
  - (d) 对同一 `command_id` 再次 authorize（批准或拒绝）→ 409，`detail` 为 `该操作已处理或当前不可授权：<command_id>`，无二次执行；不存在的 `command_id` → 404 `授权目标不存在：<command_id>`。
- **批准**：重发同请求后点 `允许`。
  - (a) `note.txt` 为 `edited`。
  - (b) `authorize` 返回 2xx；步骤徽标经 `运行中` 到 `完成`，弹窗关闭。
  - (c) 只执行一次，文件只被改一次。
  - (d) 批准后再对同一 `command_id` 拒绝 → 409，文件保持 `edited`。
- **自动放行**：先让模型写一个文件停在 `待授权`，再打开开关；然后让模型写 `auto`。
  - (a) 打开开关后原待授权调用立即被批准并执行；之后不再出现 `待授权`，文件直接变为 `auto`；`GET /api/sessions/<id>` 中 `session.auto_authorize` 为 true；`runtime_sessions/auto_authorize.json` 里该会话为 true；重启服务后刷新页面开关仍为开。
  - (b) `POST .../auto-authorize` 2xx。
  - (c) 其他会话的自动放行状态不变；`exec_policy` 判为需要授权的 `execute_command` 同样被自动放行（它也是授权工具）。
  - (d) 关掉后同类请求重新出现 `待授权`。
- **控制确认**：触发一个模型控制请求（如 extensions 中的 `propose_*` 安装类请求）。
  - (a) `GET /api/sessions/<id>` 中 `session.control_approval` 非空，含摘要与风险。
  - (b) 弹出 `等待确认`，按 `Esc`、点外部都不关闭；点 `确认` 或 `取消` 后 `POST .../control` 2xx，弹窗消失。
  - (c) `取消` 后被提议的变更不生效；重复提交与已作决定相同的选择返回原结果，不重复执行。
  - (d) 对已拒绝的请求再 `approved: true`、对已批准/已执行的请求再 `approved: false`、或用不存在的 `request_id` → 应是 409 且有 `detail`（当前源码不满足，见 Gotchas）。

## Gotchas

- `exec_policy` 是前缀清单软边界，不是只读判定：未命中清单的命令（含写文件的命令）不需授权。验证拒绝路径请用 `write_file` 或 `rm -f`。
- 没有单独的「文件管理」工具；`file_manage.py` 只注册 `write_file`。
- 自动放行只覆盖「写文件等工具」这类工具授权；控制请求（`等待确认`，如 MCP/Skill/CLI 的 `propose_*` 安装类）不受它影响，仍需确认，见 extensions。
- `等待授权` 是弹窗标题，不是回合状态；回合本身没有单独的「等待授权」状态文字。
- 授权入口的已知错误有明确状态码：失效连接 409、授权目标不存在 404、重复授权/拒绝 409；未知内部异常仍原样暴露，不做全局兜底。
- 已知产品缺口（2026-10-18 源码核对）：`/control` 的冲突（`ControlDecisionConflict`，继承 `ValueError`，`redpanda/assistant/control.py`）以及「当前没有待确认的控制操作」（`NoPendingControlApproval`）在 `app.py` 都没有异常处理器，预计返回 500 而不是 409。判据保持 409 的预期。
- 历史 live 记录：2026-10-16（main@e191eab，仅 API，界面未验证）：`write_file` 待授权 → 拒绝 200、文件未建、重复拒绝 409、无目标 404；批准后落盘；`rm -f` 进入待授权；`touch` 不需授权。部分（2026-10-12，修复后）：重复授权/无目标返回 500 已修（目标不存在 404、重复授权/拒绝 409；自动化 + 隔离 API 通过）。正常批准/拒绝的界面流程待真实 UI 重跑。2026-10-18 的修订只做源码核对，未 live 复跑。
- 已知产品缺口（2026-10-18 Windows live）：调用处于 `待授权` 时打开 `自动放行写文件等工具`，该调用被立即批准并执行，但 `等待授权` 弹窗不关闭；再点弹窗里的 `允许` → `authorize` 409，界面无任何提示后弹窗才关闭。
