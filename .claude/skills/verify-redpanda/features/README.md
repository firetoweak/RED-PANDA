# RED PANDA 验证功能地图

本目录是验证小熊猫的功能地图。先读本页，再按改动打开对应的功能文件。说明用中文写；路由、按钮文字、命令、选择器、API 路径、文件名一律照源码原样写，不翻译。

原有条目的历史验证基线与日期保留在各自状态里。2026-10-13 按当前工作区新增会话变更审阅的增量地图，不表示全量重验，也不将源码核对或自动化测试标为真实 UI 通过。

2026-10-20 按 `origin/main@32860f6` 核对全部 27 个功能文件，并以隔离 Web + 真实模型、Windows Chrome/ConPTY 对每个功能至少驱动一条路径。下表只汇总本轮覆盖；每个条目的其余子功能、错误路径与平台范围并未全验，详见各文件末尾。源码核对、自动化测试与 live 结论不相互替代。窄屏内容页隐藏输入框与原判据冲突，保留为未通过；产品代码未在维护中改动。

## 基线前置条件

编码 Skill 增量：编码方法与 Git 协作分别按需加载，普通目录支持自定义 Skill；验证见 tool-coding-round 与 extensions，既有记录不代表全部重验。

Bot 解耦增量：名册、首页、可选代码项目和通信条目已更新为无项目可用。以下表格中的维护记录仍是此前基线；当前增量的真实模型验证范围见各条目，未重新执行全部 27 项 UI 审计。

- 按 `../SKILL.md` 启动一个隔离实例：地址 `$REDPANDA_VERIFY_URL`，数据目录 `$REDPANDA_HOME`，临时工作区 `$SCRATCH_WORKSPACE`（内含 `note.txt`，内容为一行 `verify`）。
- 第一次点击前 `doctor.py` 必须对该 pid 和 home 返回 0。
- 视口至少 1280×800，侧栏才在屏幕上（窄屏时侧栏收在 `打开 Agent 名册` 后面）。
- 不要驱动 `~/.redpanda`、不要占用别人的 8765 端口、不要用带 `--workspace` 或 `--dev` 启动的服务。

## 数据落点速查（判据里反复用到）

来源 `redpanda/paths.py`、`redpanda/model_settings.py`、`redpanda/assistant/host/supervisor.py`、`redpanda/assistant/host/agent_worktrees.py`：

| 内容 | 位置 |
|---|---|
| 默认模型与候选模型 | `$REDPANDA_HOME/config.json` |
| 供应商连接（含密钥） | `$REDPANDA_HOME/connections.json` |
| 可选代码项目登记 | `$REDPANDA_HOME/workspaces.json` |
| 会话运行数据（journal、附件） | `$REDPANDA_HOME/runtime_sessions/<sha256(session_id)>/journal.sqlite` |
| 每会话模型选择 | `$REDPANDA_HOME/runtime_sessions/models.json` |
| 自动放行开关（重启后保留） | `$REDPANDA_HOME/runtime_sessions/authorization.json` |
| 定时任务 | `$REDPANDA_HOME/state/automation.sqlite` |
| 例行任务 | `$REDPANDA_HOME/state/routines.sqlite` |
| Bot 个人目录 | `$REDPANDA_HOME/state/bots/<sha256(session_id) 前 20 位>/` |
| 集成、Agent 与子任务 worktree | `$REDPANDA_HOME/state/coding_bot/<sha256(workspace_id) 前 20 位>/` 下的 `integration`、`<sha256(session_id) 前 20 位>`、`children/<sha256(子会话) 前 20 位>` |
| MCP / Skill / CLI 安装 | `$REDPANDA_HOME/mcp/`、`skills/`、`clis/` |

## 判据写法约定

每个功能文件的 `Driving it with ...` 一节里，每个子功能都给出四类可检查判据：

- **(a) 状态**：磁盘、配置文件、journal 或 `GET` 接口里应看到什么。
- **(b) 信号**：HTTP 状态码、SSE 事件、日志或界面文字。
- **(c) 不变量**：哪些东西必须保持不变。
- **(d) 错误路径**：至少一条故意走错的路径及其预期表现。

页面显示「成功」不算证据；以 (a)(c) 的实际读数为准。改动请用对应的 `GET` 复核，不要用直接 `POST` 接口代替用户操作（错误路径探测除外，判据里会写明）。

## 证据与跳过

- 证据（快照、截图、HTTP 响应体、`note.txt`）放在 `$EVIDENCE_DIR/<功能 id>/`，`note.txt` 里写功能 id 与入口。
- `live_model_ready` 为 false 时，跳过所有需要模型回复的步骤，并在 note 里写明。
- 走不到的入口就记为走不到，换一条路走通不算验证了它。

## 功能条目约定

每个功能文件：一个 H1、一段开场说明，然后按顺序四个 H2：`Sub-features`、`How to get to it (user POV)`、`Driving it with <harness>`、`Gotchas`。

## 功能列表（27 个）

| id | 文件 | 一句话 | 本轮 live 覆盖（非全部子功能） |
|---|---|---|---|
| web-session-chat | [web-session-chat.md](./web-session-chat.md) | 空首页 → 新建 Agent → 发消息 | 页面创建 Agent、真实用户输入、普通回复及刷新通过；其余见条目 |
| workspace-management | [workspace-management.md](./workspace-management.md) | `添加代码项目`、目录选择、可选关联；管理 API 保留 | 旧登记路径通过；当前界面变更与未重验范围见条目 |
| session-sidebar | [session-sidebar.md](./session-sidebar.md) | 全局 Bot 名册、无项目创建、搜索、Bot/群聊分组与状态圆点 | 无项目创建与方向/状态对照 GET 通过；其余见条目 |
| colleague-tools | [colleague-tools.md](./colleague-tools.md) | `list_colleagues`、`declare_direction`；侧栏显示最新方向；同事加入与方向事实不请求决策 | declare_direction 和 list_colleagues 真实调用通过，侧栏显示最新方向；其余见条目 |
| private-messages | [private-messages.md](./private-messages.md) | `send_message` 送达、暂停的收件人仍接纳但不推进、已知拒绝与交流上限 | Lin 向 Ada 的 send_message 送达，Ada 无新用户输入即普通回复 PRIVATE_RECEIVED；其余见条目 |
| land-and-sync | [land-and-sync.md](./land-and-sync.md) | `land_branch`、`sync_branch`；未提交不带走，冲突与挡住时集成分支或自己的提交保留 | Ada 单独 land_branch 得到 LAND_COMPLETED，集成工作树包含两个子任务成果，人的 note.txt 与 HEAD 不变；其余见条目 |
| composer-attachments | [composer-attachments.md](./composer-attachments.md) | 图片/文件拖入粘贴上传、`Add attachment` 后端本机选择、下载附件 | 有效 64×64 红色 PNG 经真实粘贴、发送、预览和 GET 下载通过，下载与原件逐字节相同；其余见条目 |
| tool-authorization | [tool-authorization.md](./tool-authorization.md) | 待处理条、批准/拒绝、默认自动放行与控制确认 | 新 Agent 默认自动放行；其余见条目 |
| inbox | [inbox.md](./inbox.md) | Bot 旁提示 → 对应对话处理授权、控制、交流超限、暂停消息与无人查看询问 | 入口精简增量：真实 write_file、Bot 提醒、会话参数展开与批准后清除通过；其余与旧版历史见条目 |
| group-chat | [group-chat.md](./group-chat.md) | 全局 `新建群聊`、群资料与成员跳转、人发言与 `点名` | 无项目建群，Ada 与 Lin 通过 send_group_message 真实发言，群页实时更新；其余见条目 |
| tool-coding-round | [tool-coding-round.md](./tool-coding-round.md) | 文件工具、前台/后台命令与非零退出 `COMMAND_EXIT_NONZERO` | read_file→apply_patch→execute_command 通过，note.txt 只改在 Ada 工作树；其余见条目 |
| session-file-changes | [session-file-changes.md](./session-file-changes.md) | Agent Git 差异摘要 → `文件改动` 内容页；子任务仅有只读接口，观察页无差异入口 | 从摘要打开文件改动，note.txt 的 verify→edited、行号和禁用分页与 GET 一致；其余见条目 |
| run-control | [run-control.md](./run-control.md) | 打断并发送、前台停止/继续、失败再试；后台命令单独停止 | 历史停止验证通过；打断并发送的增量验证见条目 |
| subagents | [subagents.md](./subagents.md) | `delegate`、`instruct_subagent`、`list_subagents`、`reclaim`、内容页 `子 Agent`、`compare_subagent`/`merge_subagent` | 真实模型多批自然回复、父主动追问和调整、状态查询、显式结束通过；项目成果与并行隔离见条目 |
| work-plan | [work-plan.md](./work-plan.md) | 计划生命周期：创建、状态标记、更新、回收（结束/中断/compact/另一个 Agent/子 Agent）、完成归档 | update_plan 建两步计划，API 留有进行中和全部完成两版；其余见条目 |
| work-plan-and-usage | [work-plan-and-usage.md](./work-plan-and-usage.md) | 轮次耗时、`上下文用量`、compact（计划部分指向 work-plan） | 真实回合的上下文用量与耗时可见，API context_input_tokens 有值；其余见条目 |
| model-settings | [model-settings.md](./model-settings.md) | `设置 → 模型配置` 抽屉、`测试`、`保存配置`、会话切换模型 | UI 添加 deepseek-flash、阈值 64000、保存和真实连接测试通过（1209 ms），未保存时测试禁用；其余见条目 |
| scheduling | [scheduling.md](./scheduling.md) | `schedule_once` / `cancel_schedule` 定时唤醒 | UI 请求 45 秒 schedule_once，倒计时可见，无新用户输入即回复 WOKE_UP；其余见条目 |
| routines | [routines.md](./routines.md) | `Agent 资料` 侧栏 `例行任务`：开关 `暂停 <名字>` / `恢复 <名字>`，`删除 <名字>` → `确认删除 <名字>`；`查看 <名字>` 看意图与执行记录 | 真实 save_routine 后列表出现；其余见条目 |
| reply-delivery | [reply-delivery.md](./reply-delivery.md) | 长期 Agent 的普通结尾直接给人看，实时与历史一致；协作消息仍须显式发送 | 普通结束正文、私信触发 PRIVATE_RECEIVED、定时触发 WOKE_UP 实时与刷新历史一致；其余见条目 |
| extensions | [extensions.md](./extensions.md) | MCP / Skill / CLI 的安装、启用、测试（对话内与 `/mcp` `/skill`） | 真实加载 MCP/Skill/CLI 管理工具并列空目录；其余见条目 |
| realtime-events | [realtime-events.md](./realtime-events.md) | `/api/events` SSE、连接状态、多标签页同步 | Doctor 首事件通过；其余见条目 |
| layout-appearance | [layout-appearance.md](./layout-appearance.md) | 三栏布局、资料栏、`外观`、侧栏宽度与窄屏名册 | 桌面资料栏/内容页替换、亮色主题刷新保持通过（theme-color=#ffffff）；窄屏内容页输入框未通过；其余见条目 |
| console-chat | [console-chat.md](./console-chat.md) | 终端单会话入口 `console_chat.py`、`/new` | 真实 Windows ConPTY 启动、/mcp list、/new、新会话 TUI_PING_OK 与 Ctrl+D 正常退出通过；其余见条目 |
| subagents-parallel | [subagents-parallel.md](./subagents-parallel.md) | 并行多个 `delegate`、worktree 隔离、并发合并冲突、收回、父会话控制与计量 | journal 确认同一决策步骤两个 delegate，独立子工作树创建 child-a/child-b，分别比较合并后父目录包含两文件；其余见条目 |
| crash-recovery | [crash-recovery.md](./crash-recovery.md) | kill -9 Web/Worker、断网、模型 429/5xx/超时/断流/无效 key 后的恢复与去重 | 认证失败后再试恢复；后续实测 Worker 被杀后再试不重复执行、输入重放冲突 409 且后续对话可用；其余见条目 |
| setup-and-launch | [setup-and-launch.md](./setup-and-launch.md) | `setup.sh`/`setup.ps1` 四步准备、模型向导、`start.sh`/`dev.sh` | Windows setup 四步、setup --check、隔离 Web 启动与 Doctor 全部通过，浏览器首页可用；其余见条目 |

## 入口 → 地图条目 覆盖表

### Web 路由（`web/src/app/App.tsx`）

| 入口 | 条目 |
|---|---|
| `/` → `AgentHome` | session-sidebar、web-session-chat |
| `/workspaces/:workspaceId` → `AgentHome` | session-sidebar、workspace-management |
| `/sessions/:sessionId` → `SessionConversation` | web-session-chat、inbox 及所有会话类条目 |
| `/groups/:groupId` → `GroupChatPage` | group-chat |
| 侧栏 `设置 → 模型配置` / `共同记忆` → `SettingsMenu` 抽屉（保持聊天路由） | model-settings、colleague-tools |
| 未知前端路径回落 `index.html`（`app.py` 的 `_FrontendFiles`）；未知 API 保持 404 | web-session-chat |

### HTTP / SSE 接口（`redpanda/channels/web/app.py`）

| 接口 | 条目 |
|---|---|
| `GET /api/events` | realtime-events、reply-delivery（流式预览与最终回复） |
| `GET/PUT /api/model-settings`、`POST /api/model-settings/test` | model-settings |
| `GET/PUT /api/shared-memory` | colleague-tools |
| `GET/PUT /api/sessions/{id}/model` | model-settings |
| `GET /api/agents`、`POST /api/agents` | session-sidebar、colleague-tools |
| `GET /api/sessions/{id}` | web-session-chat、reply-delivery（普通回复与文件附件） |
| `GET /api/sessions/{id}/commands`、`POST /api/sessions/{id}/commands/{command_id}/stop` | tool-coding-round |
| `GET /api/sessions/{id}/routines`、`PUT /api/sessions/{id}/routines/{routine_id}/state` | routines |
| `GET /api/inbox` | inbox |
| `GET/POST /api/groups`、`GET /api/groups/{group_id}`、`POST /api/groups/{group_id}/messages` | group-chat |
| `GET /api/sessions/{id}/changes`（仅 Agent 与其委派任务；摘要；`path`/`revision`/`offset` 查询单文件差异） | session-file-changes |
| `POST /api/sessions/{id}/select` | session-sidebar、web-session-chat |
| `GET/POST /api/workspaces`、`PATCH/DELETE /api/workspaces/{workspace_id}`、`POST /api/workspaces/select-directory` | workspace-management |
| `POST .../attachments`（拖入上传）、`POST .../attachments/select`（后端本机选择）、`GET .../attachments/{attachment_id}` | composer-attachments |
| `POST /api/sessions/{id}/inputs` | web-session-chat |
| `POST .../commands/{command_id}/authorize`、`POST .../auto-authorize`、`POST .../control` | tool-authorization |
| `POST .../cancel`、`POST .../retry` | run-control、crash-recovery（故障后重试） |
| `GET .../subagents/{command_id}` | subagents、subagents-parallel |
| `/assets/*`、`/` 静态挂载与前端回落 | web-session-chat |

### 模型可调用的工具（Assistant 能力，经对话触发）

| 工具 | 来源 | 条目 |
|---|---|---|
| `read_file` `glob` `grep` `write_file` `apply_patch` `replace_all` `execute_command` | `redpanda/tools/builtin/` | tool-coding-round |
| `delegate` `instruct_subagent` `list_subagents` `reclaim` `compare_subagent` `merge_subagent` | `redpanda/assistant/subagent/subagent.py`、`tools/builtin/subagent_workspace.py` | subagents、subagents-parallel |
| `read_image` | `redpanda/assistant/attachments.py` | composer-attachments |
| `update_plan` | `redpanda/assistant/work_plan.py` | work-plan |
| `find_history`（`model_usage` 不是工具，是用量元数据键，模型调不到） | `redpanda/assistant/compact/core.py` | work-plan-and-usage |
| `read_command` `stop_command` `list_commands`（后台命令，Linux/Windows） | `redpanda/tools/builtin/background_commands.py` | tool-coding-round |
| `schedule_once` `cancel_schedule` | `redpanda/automation/tool.py` | scheduling |
| `save_routine` `set_routine_state` `list_routines` | `redpanda/automation/routine_tool.py` | routines |
| `list_colleagues` `declare_direction` | `redpanda/assistant/agent_messages.py` | colleague-tools |
| `send_message` | `redpanda/assistant/agent_messages.py` | private-messages |
| `send_group_message` | `redpanda/assistant/agent_messages.py` | group-chat |
| `land_branch` `sync_branch` | `redpanda/assistant/agent_tools.py` | land-and-sync |
| `send_file`，以及长期 Agent 的普通回复直接给人看 | `redpanda/assistant/agent_messages.py`、`redpanda/assistant/conversations.py` | reply-delivery |
| `load_management_tools` `load_toolset` `list_mcp_servers` `test_mcp_server` `propose_mcp_*` `load_skill` `read_skill_resource` `list_installed_skills` `test_installed_skill` `propose_skill_*` `load_cli` `list_installed_clis` `inspect_cli` `test_cli` `propose_cli_*` | `redpanda/mcp/`、`redpanda/skills/`、`redpanda/cli/`、`redpanda/assistant/management.py` | extensions |
| `read_content` | `redpanda/assistant/content.py` | tool-coding-round（读取或检索工具结果/历史返回的真实引用） |

### 命令行与脚本

| 入口 | 条目 |
|---|---|
| `web_chat.py [--dev] [--host] [--port] [--workspace]` | web-session-chat（`--workspace` 见 workspace-management Gotchas） |
| `console_chat.py [--home] [--workspace]`，会内 `/new` `/mcp ...` `/skill ...` | console-chat、extensions |
| `scripts/setup.sh`、`scripts/setup.ps1`、`scripts/setup.py [--configure] [--check]` | setup-and-launch |
| `python -m redpanda.initialize --configure / --if-needed` | setup-and-launch |
| `scripts/start.sh`、`scripts/start.ps1`（→ `scripts/launch.py` → `web_chat.py`） | setup-and-launch |
| `scripts/dev.sh`、`scripts/dev.ps1`（→ `scripts.launch --dev`） | setup-and-launch |

### 非入口类场景（故障与边界）

| 场景 | 源码 | 条目 |
|---|---|---|
| Worker 失败 → SSE `session_failed` | `redpanda/channels/web/app.py` `report_worker_failures`、`assistant/host/supervisor.py` | crash-recovery |
| 未完成命令 `CommandPhase.UNKNOWN` / `NO_RESULT_YET` | `runtime/state.py`、`assistant/context/projection.py`、`assistant/conversations.py` | crash-recovery |
| 模型错误分类与重试 | `thinllm/chat_completions.py`、`assistant/failures.py` | crash-recovery |
| 输入去重 `delivery_id` | `runtime/journal/sqlite.py` | crash-recovery |
| Agent 与委派任务的 Git 差异、差异版本变化 | `assistant/conversations.py`、`assistant/agent_git.py`、`web/src/features/conversation/FileChangesPanel.tsx` | session-file-changes |
| 子 Agent 重启接管与中断回传 | `assistant/subagent/subagent.py` `rehydrate`、`record_interrupted_return` | subagents-parallel、crash-recovery |
| compact 窗口继承 | `assistant/compact/core.py` | work-plan-and-usage |
| 普通正文交付（`deliver` 是内部命令，模型不能调用） | `redpanda/assistant/delivery.py` | reply-delivery、web-session-chat |

### 有意不覆盖

- `thinllm/`：底层 LLM 适配库，不是用户入口；经 model-settings 的 `测试` 间接覆盖。
- `redpanda/runtime/journal` 的内部恢复逻辑：用户不可直接触发；判据里通过 `GET /api/sessions/{id}` 读 journal 投影。


## 历史轮次

以下轮次记录保留当时事实。2026-10-18 起文件回退整体移除：agent-workspace-restore、file-rewind、rewind-edge-cases、edit-and-branch 已删除，setup-and-sandbox-install 改为 setup-and-launch，Doctor 不再检查 `sandbox.ready`。

## 2026-10-12 live 实跑
证据根目录：`/workspace/verify-run-20261012`（下文路径相对于它），完整报告见该目录 `REPORT.md`。

该轮次对应源码基线 `main@1a4790a`。其后的 2026-10-12 修复轮次已把落地契约写入对应架构文档（如 `docs/架构/入口/Web.md` 与当时的文件操作与回退文档）：隔离 API 复跑记录在仓库 `.tools/verification/fixapi-20261012/`（16 项检查，覆盖附件入口边界、已知入口错误、模型失败可见与再试）；真实 UI 流程未在该环境复跑，本表中相关条目标注「UI 待复跑」。

## 2026-10-16 维护轮次

源码基线 `main@e191eab`，隔离实例 + DeepSeek 真模型 + Linux 沙箱，经 HTTP/SSE/磁盘驱动（本轮无浏览器，所有条目「界面未验证」）。修正：doctor 沙箱路径、工作区重命名/移除、持续进程工具、`execute_command` 授权范围、回滚产生新会话、`model_usage` 非工具、安装脚本参数与前置。表中旧状态未逐条改写。

## 2026-10-18 源码核对轮次

源码基线 `main@b7a21c3`。25 个功能文件逐个对照源码核对并改写，**没有驱动 Web 页面**：表中「live 验证状态」沿用旧记录，各文件 `Gotchas` 里的历史验证条目注明「2026-10-18 按源码改写，未 live 复跑」。主要漂移来自统一内容页（`文件改动`、`子 Agent`）、停止与再试语义、模型可见文案压缩、回滚结果提示上线和 `skill_help` 删除。各文件原有的额外 H2（`Live status` 等）已并入 `Gotchas`。

Harness 修正已在 Windows 上按 `../SKILL.md` 原文实跑 Launch → Doctor → Cleanup：`doctor.py` 在 Windows 上不再用 `os.kill(pid, 0)`（该调用在 Windows 等于发送 Ctrl+C），改为查询进程状态并核对命令行，并检查 Windows 命令执行组件；Windows 启动片段改为可直接运行的 PowerShell。Linux 上的 Doctor 路径未改动、未在本轮复跑。

源码核对发现、待 live 确认或修复的产品缺口（记录在对应文件 `Gotchas`，判据保持正确预期）：

- web-session-chat（历史缺口）：未知 API 被静态兜底吞掉、静态目录缺少路径约束；当前已改用标准 StaticFiles，2026-10-20 对照测试通过，尚待 Web live 复验。
- tool-authorization：`/control` 决定冲突与无待确认请求没有异常处理器，返回 500 而非 409。
- crash-recovery（已修复）：同一 `delivery_id` 改文本重放，2026-10-20 live 确认修复前为 500 + Worker 失败，修复后为 409、原事件不变且后续对话可用。历史 Worker 卡死在同日后续实测未复现，范围见条目。
- workspace-management：空名称创建工作区可能 500（界面已拦）。
- extensions：`/mcp help` 列出 `/mcp reload`，实际没有该子命令。另记一项待定：终端 `/mcp upsert` 不经过模型路径上的 Shell 解释器拦截，是否应拦截由产品决定。
- composer-attachments：隐藏的文件 `input` 没有任何入口触发。

## 2026-10-18 Windows live 轮次

同日在 Windows 上按 `../SKILL.md` 隔离启动实跑（`setup.ps1` 全流程、Doctor `--require-model --require-sandbox` 通过、模型 `deepseek/deepseek-flash`、agentFS 0.5.1），用桌面 app 内置浏览器面板驱动，证据（`note.txt`、`state.json`、`page*.jpg`）留在本机证据目录。本节仅记录移除前版本的 live 结果，不作为当前版本通过的依据；执行方式与判据有变化的条目需重新 live 验证。

| 结果 | 功能 |
|---|---|
| 通过 | web-session-chat、tool-authorization、session-file-changes、run-control、subagents、subagents-parallel（并行派发）、scheduling、model-settings、session-sidebar、realtime-events、work-plan、work-plan-and-usage |
| 失败（产品缺口） | tool-coding-round（Windows 退出码折叠为 1）、crash-recovery（Worker 崩溃后会话永久卡死） |
| 走不到 | workspace-management（重命名/移除）、layout-appearance（外观菜单）、composer-attachments、extensions、console-chat——中途浏览器面板隐藏后 `requestAnimationFrame`/`ResizeObserver` 不触发（实测 0 次），弹窗、菜单与虚拟时间线无法渲染；console-chat 需要交互终端 |

当时记为通过、现已从地图删除：agent-workspace-restore、file-rewind、rewind-edge-cases、edit-and-branch。setup-and-sandbox-install 已改为 setup-and-launch。

本轮 live 新发现的产品缺口（已写入对应文件 `Gotchas`）：Windows 命令退出码折叠为 1；Worker 崩溃后会话永久卡死；自动放行批准后 `等待授权` 弹窗不关闭（已随待处理条消除）；会话页高频轮询 `/model` 与 `/model-settings`。地图漂移已按实测修正：停止命令后不出现 `继续`（run-control）、SSE keep-alive 实际为 `: ping`（realtime-events）。

## 已确认的设计边界（不当问题报）

- Web API 没有登录鉴权，是刻意设计。
- `文件改动` 只对 Agent 与其委派任务提供，内容是 Git 差异，不区分会话外修改；外部编辑暂需手动刷新。没有系统内文件回退。
- 工作计划由模型制定和控制，没有完整状态机。
- `select-directory` 是 human-in-the-loop，人不选就一直等。
- 停止：只有一个 `停止`；一次性写入写完再停，长命令杀进程组，中断调用标 unknown 并给模型简短提示，`继续` 不自动重跑。

