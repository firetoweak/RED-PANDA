# konden 验证功能地图

本目录是验证konden的功能地图。先读本页，再按改动打开对应的功能文件。说明用中文写；路由、按钮文字、命令、选择器、API 路径、文件名一律照源码原样写，不翻译。

原有条目的历史验证基线与日期保留在各自状态里。2026-10-13 按当前工作区新增会话变更审阅的增量地图，不表示全量重验，也不将源码核对或自动化测试标为真实 UI 通过。

2026-10-22 按 `origin/main@4c000ca` 核对全部 27 个功能文件，使用同一个隔离 Web 实例、真实 DeepSeek 和 Windows Chrome，每个功能至少实跑一条路径；终端另用独立 home 与 ConPTY。下表与条目末尾记录本轮覆盖，不代表所有子功能和错误路径都通过。窄屏内容页隐藏输入框仍是产品缺口，维护未修改产品代码。

## 基线前置条件

编码 Skill 增量：编码方法与 Git 协作分别按需加载，普通目录支持自定义 Skill；验证见 tool-coding-round 与 extensions，既有记录不代表全部重验。

Bot 可不挂项目独立使用；代码任务需显式选择项目。名册以 `projects` 列表描述挂载关系，落地、同步需要项目名；子任务共享父工作区，委派可选择已有工作目录。

- 按 `../SKILL.md` 启动一个隔离实例：地址 `$KONDEN_VERIFY_URL`，数据目录 `$KONDEN_HOME`，临时工作区 `$SCRATCH_WORKSPACE`（内含 `note.txt`，内容为一行 `verify`）。
- 第一次点击前 `doctor.py` 必须对该 pid 和 home 返回 0。
- 视口至少 1280×800，侧栏才在屏幕上（窄屏时侧栏收在 `打开 Agent 名册` 后面）。
- 不要驱动 `~/.konden`、不要占用别人的 8765 端口、不要用带 `--workspace` 或 `--dev` 启动的服务。

## 数据落点速查（判据里反复用到）

来源 `konden/paths.py`、`konden/model_settings.py`、`konden/assistant/host/supervisor.py`、`konden/assistant/host/agent_worktrees.py`：

| 内容 | 位置 |
|---|---|
| 默认模型与候选模型 | `$KONDEN_HOME/config.json` |
| 供应商连接（含密钥） | `$KONDEN_HOME/connections.json` |
| 可选代码项目登记 | `$KONDEN_HOME/workspaces.json` |
| 会话运行数据（journal、附件） | `$KONDEN_HOME/runtime_sessions/<sha256(session_id)>/journal.sqlite` |
| 每会话模型选择 | `$KONDEN_HOME/runtime_sessions/models.json` |
| 自动放行开关（重启后保留） | `$KONDEN_HOME/runtime_sessions/authorization.json` |
| 定时任务 | `$KONDEN_HOME/state/automation.sqlite` |
| 例行任务 | `$KONDEN_HOME/state/routines.sqlite` |
| Bot 个人目录 | `$KONDEN_HOME/state/bots/<sha256(session_id) 前 20 位>/` |
| Bot 的项目 worktree | Bot 个人目录下 `projects/<项目名>/` |
| 集成 worktree | `$KONDEN_HOME/state/coding_bot/<sha256(workspace_id) 前 20 位>/integration` |
| MCP / Skill / CLI 安装 | `$KONDEN_HOME/mcp/`、`skills/`、`clis/` |

## 判据写法约定

每个功能文件的 `Driving it with ...` 一节里，每个子功能都给出四类可检查判据：

- **(a) 状态**：磁盘、配置文件、journal 或 `GET` 接口里应看到什么。
- **(b) 信号**：HTTP 状态码、SSE 事件、日志或界面文字。
- **(c) 不变量**：哪些东西必须保持不变。
- **(d) 错误路径**：至少一条故意走错的路径及其预期表现。

页面显示「成功」不算证据；以 (a)(c) 的实际读数为准。改动请用对应的 `GET` 复核，不要用直接 `POST` 接口代替用户操作（错误路径探测除外，判据里会写明）。

已经落地的边界用例留在所属功能的 `Driving it with ...` 中，记录可控触发、验收不变量和自动化入口，作为后续相关开发的复用基线；单次日期、结果、证据与未覆盖范围留在 `Gotchas`。例如 [实时响应时序基线](realtime-events.md#driving-it-with-the-browser) 覆盖旧快照晚到、旧选择确认、Bot／群切换和重连流式交错。修改相关流程时先复跑受影响的既有场景，新增用例仍须守住一条有实际外部失败源的契约，不按清单无差别扩张测试。

## 证据与跳过

- 证据（快照、截图、HTTP 响应体、`note.txt`）放在 `$EVIDENCE_DIR/<功能 id>/`，`note.txt` 里写功能 id 与入口。
- `live_model_ready` 为 false 时，跳过所有需要模型回复的步骤，并在 note 里写明。
- 走不到的入口就记为走不到，换一条路走通不算验证了它。

## 功能条目约定

每个功能文件：一个 H1、一段开场说明，然后按顺序四个 H2：`Sub-features`、`How to get to it (user POV)`、`Driving it with <harness>`、`Gotchas`。

## 功能列表（28 个）

| id | 文件 | 一句话 | 本轮 live 覆盖（非全部子功能） |
|---|---|---|---|
| web-session-chat | [web-session-chat.md](./web-session-chat.md) | 空首页 → 新建 Agent → 发消息 | 无项目 Bot 真实发送与回复、刷新历史通过；长历史、引用跳转与异常 API 未重验。 |
| workspace-management | [workspace-management.md](./workspace-management.md) | 创建 Bot 同页选目录、按目录名展示、复用与后续关联 | 2026-10-23 增量：取消不登记、两位 Bot 复用同目录、后续关联同名仓库、路径区分、错误草稿保留及桌面与窄屏通过；原生目录窗口与解除未重验。 |
| session-sidebar | [session-sidebar.md](./session-sidebar.md) | 全局 Bot 名册、无项目创建、搜索、Bot/群聊分组与状态圆点 | 2026-10-26 首次引导：无模型时只显示模型配置且不能开始，配置可用后预填名字可改，开始后进入对话并由新 Bot 主动介绍（入口说法与界面一致）；删除唯一 Bot 后引导重新出现。此前无项目 Ada 与有项目 Lin 创建、名册切换和状态对照 GET 通过；分组与搜索未重验。 |
| colleague-tools | [colleague-tools.md](./colleague-tools.md) | 名册工具与加入事实；共同／个人工作约束的工具读写与页面编辑 | 2026-10-27 观察追加增量：真实页面和模型验证连续轮次、共同／个人更新、Compact 后续接与清空的实际请求前缀；隔离和并发草稿沿用原轮次页面及自动化证据。 |
| private-messages | [private-messages.md](./private-messages.md) | `send_message` 送达、停止执行的收件人仍接纳但不推进、已知拒绝与交流上限 | 有项目 Lin 向无项目 Ada 送达 PRIVATE_MAP_OK，双方可见来源与跳转；停止执行接收和交流上限未重验。 |
| external-conversations | [external-conversations.md](./external-conversations.md) | 原生聊天独立身份、通用双向消息与 Web 对话 | 2026-10-25 Windows、真实 DeepSeek：登记可见、双身份分流、主动后续、重启续读与撤销历史通过；原生客户端界面与 Linux 未测。 |
| land-and-sync | [land-and-sync.md](./land-and-sync.md) | `land_branch`、`sync_branch`；未提交不带走，冲突与挡住时集成分支或自己的提交保留 | 真实指定 project 落地后同步，包含两个子任务成果，差异清零且人的原始 note.txt 不变；同步他人新增成果、冲突和阻挡未重验。 |
| composer-attachments | [composer-attachments.md](./composer-attachments.md) | 图片/文件拖入粘贴上传、`Add attachment` 后端本机选择、下载附件 | 有效 64×64 PNG 真实粘贴、发送、刷新预览通过，GET 下载与原件逐字节相同；本机选择、普通文件、跨会话边界未重验。 |
| tool-authorization | [tool-authorization.md](./tool-authorization.md) | 待处理条、批准/拒绝、默认自动放行与控制确认 | 全局关闭自动放行后 write_file 等待，展开参数并允许后执行一次，待处理消失；拒绝与控制确认未重验。 |
| inbox | [inbox.md](./inbox.md) | Bot 旁提示 → 对应对话处理授权、控制、交流超限、停止执行消息与无人查看询问 | 名册提示与会话待处理参数、批准后清除通过；交流超限、停止执行积压与长时间无人查看未重验。 |
| group-chat | [group-chat.md](./group-chat.md) | 全局 `新建群聊`、群资料与成员跳转、人发言与 `点名` | 创建两成员群、@all 补全、人发言及两个 Bot 的真实群回复实时显示通过；成员改动与群回复引用跳转未重验。 |
| tool-coding-round | [tool-coding-round.md](./tool-coding-round.md) | 文件工具、前台/后台命令与非零退出 `COMMAND_EXIT_NONZERO` | 真实 read_file/apply_patch/write_file/execute_command 通过，文件只改在挂载项目内；后台命令与非零退出未重验。 |
| run-control | [run-control.md](./run-control.md) | 打断并发送、前台打断、失败再试；后台命令单独停止 | 子助手已回复但协作未结束时，父空闲显示发送与停止；显式回收后停止消失；认证失败再试通过。 |
| subagents | [subagents.md](./subagents.md) | `delegate`、`instruct_subagent`、`list_subagents`、`reclaim`、内容页 `子 Agent`、共享文件、命令授权与资源回收 | 2026-10-26 Windows Chrome、受控模型：无项目共享写入、子命令在父处授权、运行中插话与显式回收通过；真实供应商及多子并行未重验。 |
| work-plan | [work-plan.md](./work-plan.md) | 计划生命周期：创建、状态标记、更新、回收（结束/中断/compact/另一个 Agent/子 Agent）、完成归档 | 真实 update_plan 建两步计划并全部完成，展开归档与 GET 一致；中断、compact 和跨 Agent 回收未重验。 |
| work-plan-and-usage | [work-plan-and-usage.md](./work-plan-and-usage.md) | 消息时间、`上下文用量`、compact（计划部分指向 work-plan） | 真实回合时间与上下文用量环、200K 阈值和 GET 一致，不再有轮次耗时；compact 未触发。 |
| model-settings | [model-settings.md](./model-settings.md) | `设置 → 模型配置` 抽屉、`测试`、`保存配置`、会话切换模型 | 设置抽屉编辑当前模型阈值，加入待保存配置并保存，GET 确认生效；连接测试、模型切换和表单边界未重验。 |
| scheduling | [scheduling.md](./scheduling.md) | `schedule_once` / `cancel_schedule` 定时唤醒 | 真实 schedule_once 45 秒倒计时可见，无新用户输入即 MAP_WAKE；取消、重启持久化与错参未重验。 |
| routines | [routines.md](./routines.md) | `Bot 资料` 侧栏／窄屏 `Agent 资料` 抽屉的 `例行任务` 页签：开关 `暂停 <名字>` / `恢复 <名字>`，`删除 <名字>` → `确认删除 <名字>`；`查看 <名字>` 看意图与执行记录 | 真实 save_routine，1100 宽度资料抽屉查看与暂停，状态变为人暂停且下次未安排；删除、失败和无人查看策略未重验。 |
| reply-delivery | [reply-delivery.md](./reply-delivery.md) | 长期 Agent 的普通结尾直接给人看，实时与历史一致；协作消息仍须显式发送 | 普通回复、私信与定时触发的结果可见，刷新后原文一致；正文右键复制消息的剪贴板为原文。send_file 未重验。 |
| extensions | [extensions.md](./extensions.md) | 侧栏 `能力` 清单、MCP / Skill / CLI 的安装、启用、测试（对话内与 `/mcp` `/skill`） | 隔离 Web 的能力清单、搜索、展开、启停与读取失败重试通过；真实模型的安装、测试、控制提案和移除未重验。 |
| realtime-events | [realtime-events.md](./realtime-events.md) | `/api/events` SSE、连接状态、多标签页同步 | Doctor 首事件与双标签实时状态、全局自动授权同步通过；保活时间、断线重连和失效连接未重验。 |
| layout-appearance | [layout-appearance.md](./layout-appearance.md) | 三栏布局、资料栏、`外观`、侧栏宽度与窄屏名册 | 亮色主题刷新保持、桌面内容页和 1100 资料抽屉通过；390 窄屏内容页仍隐藏输入框，作为产品缺口保留。 |
| console-chat | [console-chat.md](./console-chat.md) | 终端单会话入口 `console_chat.py`、`/new` | 真实 Windows ConPTY 启动、/mcp list、/new、新会话 TUI_PING_OK 与 Ctrl+D 退出通过；更新终端说明后另建 home 复跑。粘贴、底栏和并发占用未重验。 |
| subagents-parallel | [subagents-parallel.md](./subagents-parallel.md) | 并行委派、共享文件、选择性回收与父会话控制 | 2026-10-26 按共享工作区更新；多子并行页面未重验，单子执行与回收见 subagents。 |
| crash-recovery | [crash-recovery.md](./crash-recovery.md) | kill -9 Web/Worker、断网、模型 429/5xx/超时/断流/无效 key 后的恢复与去重 | 隔离配置无效 key 触发认证失败，刷新横幅仍在，恢复配置后再试成功且原输入仅一条；杀进程、断网、429 和未知命令恢复未重验。 |
| setup-and-launch | [setup-and-launch.md](./setup-and-launch.md) | `setup.sh`/`setup.ps1` 四步准备、模型向导、`start.sh`/`dev.sh` | Windows setup 四步、setup --check、隔离 Web 启动、Doctor 与首页通过；Linux、首次模型向导和开发启动未重验。 |

## 入口 → 地图条目 覆盖表

### Web 路由（`web/src/app/App.tsx`）

| 入口 | 条目 |
|---|---|
| `/` → `AgentHome` | session-sidebar、web-session-chat |
| `/capabilities` → `CapabilitiesPage` | extensions |
| `/sessions/:sessionId` → `SessionConversation` | web-session-chat、inbox 及所有会话类条目 |
| `/groups/:groupId` → `GroupChatPage` | group-chat |
| 侧栏 `设置 → 模型配置` / `共同记忆` → `SettingsMenu` 抽屉（保持聊天路由） | model-settings、colleague-tools |
| 未知前端路径回落 `index.html`（`app.py` 的 `_FrontendFiles`）；未知 API 保持 404 | web-session-chat |

### HTTP / SSE 接口（`konden/channels/web/app.py`）

| 接口 | 条目 |
|---|---|
| `GET /api/events` | realtime-events、reply-delivery（流式预览与最终回复） |
| `GET /api/capabilities`、`PUT /api/capabilities/{kind}/{id}/enabled`（kind 为 skills 或 mcp） | extensions |
| `GET/PUT /api/model-settings`、`POST /api/model-settings/test` | model-settings |
| `GET/PUT /api/shared-memory` | colleague-tools |
| `GET/PUT /api/sessions/{id}/model` | model-settings |
| `GET /api/agents`、`POST /api/agents` | session-sidebar、colleague-tools |
| `POST /api/agents/{id}/projects`、`POST /api/agents/{id}/projects/{name}/unlink` | workspace-management |
| `GET /api/sessions/{id}` | web-session-chat、reply-delivery（普通回复与文件附件） |
| `GET /api/sessions/{id}/commands`、`POST /api/sessions/{id}/commands/{command_id}/stop` | tool-coding-round |
| `GET /api/sessions/{id}/routines`、`PUT /api/sessions/{id}/routines/{routine_id}/state` | routines |
| `PUT /api/agents/{id}/name` | session-sidebar |
| `PUT /api/agents/{id}/direction` | session-sidebar、colleague-tools |
| `GET /api/inbox` | inbox |
| `GET/POST /api/groups`、`GET /api/groups/{group_id}`、`POST /api/groups/{group_id}/messages` | group-chat |
| `POST /api/sessions/{id}/select` | session-sidebar、web-session-chat |
| `GET /api/workspaces`、`POST /api/workspaces/select-directory` | workspace-management |
| `POST .../attachments`（拖入上传）、`POST .../attachments/select`（后端本机选择）、`GET .../attachments/{attachment_id}` | composer-attachments |
| `POST /api/sessions/{id}/inputs` | web-session-chat |
| `POST .../commands/{command_id}/authorize`、`GET/POST /api/auto-authorize`、`POST .../control` | tool-authorization |
| `POST .../cancel`、`POST .../retry` | run-control、crash-recovery（故障后重试） |
| `GET .../subagents`、`GET .../subagents/{command_id}` | subagents、subagents-parallel |
| `/assets/*`、`/` 静态挂载与前端回落 | web-session-chat |

### 模型可调用的工具（Assistant 能力，经对话触发）

| 工具 | 来源 | 条目 |
|---|---|---|
| `read_file` `glob` `grep` `write_file` `apply_patch` `replace_all` `execute_command` | `konden/tools/builtin/` | tool-coding-round |
| `delegate` `instruct_subagent` `list_subagents` `reclaim` | `konden/assistant/subagent/subagent.py` | subagents、subagents-parallel |
| `read_image` | `konden/assistant/attachments.py` | composer-attachments |
| `update_plan` | `konden/assistant/work_plan.py` | work-plan |
| `find_history`（`model_usage` 不是工具，是用量元数据键，模型调不到） | `konden/assistant/compact/core.py` | work-plan-and-usage |
| `read_command` `stop_command` `list_commands`（后台命令，Linux/Windows） | `konden/tools/builtin/background_commands.py` | tool-coding-round |
| `schedule_once` `cancel_schedule` | `konden/automation/tool.py` | scheduling |
| `save_routine` `set_routine_state` `list_routines` | `konden/automation/routine_tool.py` | routines |
| `list_colleagues` | `konden/assistant/agent_messages.py` | colleague-tools |
| `read_shared_memory` `update_shared_memory` | `konden/assistant/shared_memory.py` | colleague-tools |
| `send_message` | `konden/assistant/agent_messages.py` | private-messages |
| 全局设置「Codex 接入」、侧栏「外部会话」；MCP 身份与消息；Bot 外部查询和发送 | `features/settings/ExternalAccess.tsx`、`features/external/`、`konden/channels/mcp/`、`assistant/host/external_messages.py` | external-conversations |
| `send_group_message` | `konden/assistant/agent_messages.py` | group-chat |
| `link_project` `unlink_project` | `konden/assistant/agent_tools.py` | workspace-management |
| `land_branch` `sync_branch` | `konden/assistant/agent_tools.py` | land-and-sync |
| `send_file`，以及长期 Agent 的普通回复直接给人看 | `konden/assistant/agent_messages.py`、`konden/assistant/conversations.py` | reply-delivery |
| `load_management_tools` `load_toolset` `list_mcp_servers` `test_mcp_server` `propose_mcp_*` `load_skill` `read_skill_resource` `list_installed_skills` `test_installed_skill` `propose_skill_*` `load_cli` `list_installed_clis` `inspect_cli` `test_cli` `propose_cli_*` | `konden/mcp/`、`konden/skills/`、`konden/cli/`、`konden/assistant/management.py` | extensions |
| `read_content` | `konden/assistant/content.py` | tool-coding-round（读取或检索工具结果/历史返回的真实引用） |

### 命令行与脚本

| 入口 | 条目 |
|---|---|
| `web_chat.py [--dev] [--host] [--port] [--workspace]` | web-session-chat（`--workspace` 见 workspace-management Gotchas） |
| `console_chat.py [--home] [--workspace]`，会内 `/new` `/mcp ...` `/skill ...` | console-chat、extensions |
| `scripts/setup.sh`、`scripts/setup.ps1`、`scripts/setup.py [--configure] [--check]` | setup-and-launch |
| `python -m konden.initialize --configure / --if-needed` | setup-and-launch |
| `scripts/start.sh`、`scripts/start.ps1`（→ `scripts/launch.py` → `web_chat.py`） | setup-and-launch |
| `scripts/dev.sh`、`scripts/dev.ps1`（→ `scripts.launch --dev`） | setup-and-launch |

### 非入口类场景（故障与边界）

| 场景 | 源码 | 条目 |
|---|---|---|
| Worker 失败 → SSE `session_failed` | `konden/channels/web/app.py` `report_worker_failures`、`assistant/host/supervisor.py` | crash-recovery |
| 未完成命令 `CommandPhase.UNKNOWN` / `NO_RESULT_YET` | `runtime/state.py`、`assistant/context/projection.py`、`assistant/conversations.py` | crash-recovery |
| 模型错误分类与重试 | `thinllm/chat_completions.py`、`assistant/failures.py` | crash-recovery |
| 输入去重 `delivery_id` | `runtime/journal/sqlite.py` | crash-recovery |
| 子 Agent 重启接管与中断回传 | `assistant/subagent/subagent.py` `rehydrate`、`record_interrupted_return` | subagents-parallel、crash-recovery |
| compact 窗口继承 | `assistant/compact/core.py` | work-plan-and-usage |
| 普通正文交付（`deliver` 是内部命令，模型不能调用） | `konden/assistant/delivery.py` | reply-delivery、web-session-chat |

### 有意不覆盖

- `thinllm/`：底层 LLM 适配库，不是用户入口；经 model-settings 的 `测试` 间接覆盖。
- `konden/runtime/journal` 的内部恢复逻辑：用户不可直接触发；判据里通过 `GET /api/sessions/{id}` 读 journal 投影。


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
- `文件改动` 只对挂项目的 Bot提供，内容是 Git 差异，不区分会话外修改；外部编辑暂需手动刷新。没有系统内文件回退。
- 工作计划由模型制定和控制，没有完整状态机。
- `select-directory` 是 human-in-the-loop，人不选就一直等。
- 停止：只有一个 `停止`；一次性写入写完再停，长命令杀进程组，中断调用标 unknown 并给模型简短提示，`继续` 不自动重跑。

