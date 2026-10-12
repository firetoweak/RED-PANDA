# RED PANDA 验证功能地图（完整版，基于 main@1a4790a）

本目录是验证小熊猫的功能地图。先读本页，再按改动打开对应的功能文件。说明用中文写；路由、按钮文字、命令、选择器、API 路径、文件名一律照源码原样写，不翻译。

## 基线前置条件

- 按 `../SKILL.md` 启动一个隔离实例：地址 `$REDPANDA_VERIFY_URL`，数据目录 `$REDPANDA_HOME`，临时工作区 `$SCRATCH_WORKSPACE`（内含 `note.txt`，内容为一行 `verify`）。
- 第一次点击前 `doctor.py` 必须对该 pid 和 home 返回 0。
- 视口至少 1280×800，侧栏才在屏幕上（窄屏时侧栏收在 `打开会话列表` 后面）。
- 不要驱动 `~/.redpanda`、不要占用别人的 8765 端口、不要用带 `--workspace` 或 `--dev` 启动的服务。

## 数据落点速查（判据里反复用到）

来源 `redpanda/paths.py`、`redpanda/model_settings.py`、`redpanda/assistant/host/supervisor.py`、`redpanda/sandbox/files/children.py`：

| 内容 | 位置 |
|---|---|
| 默认模型与候选模型 | `$REDPANDA_HOME/config.json` |
| 供应商连接（含密钥） | `$REDPANDA_HOME/connections.json` |
| 工作区登记 | `$REDPANDA_HOME/workspaces.json` |
| 会话运行数据（journal、附件、文件版本） | `$REDPANDA_HOME/runtime_sessions/` |
| 每会话模型选择 | `$REDPANDA_HOME/runtime_sessions/models.json` |
| 定时任务 | `$REDPANDA_HOME/state/automation.sqlite` |
| 子 Agent 工作副本 / 文件交换 | `$REDPANDA_HOME/state/subagent_worktrees/`、`state/file_exchanges/` |
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
- `sandbox.ready` 为 false 时，跳过文件工具、回滚、子 Agent 合并。macOS 没有文件视图，始终跳过。
- 走不到的入口就记为走不到，换一条路走通不算验证了它。

## 功能条目约定

每个功能文件：一个 H1、一段开场说明，然后按顺序四个 H2：`Sub-features`、`How to get to it (user POV)`、`Driving it with <harness>`、`Gotchas`。

## 功能列表（24 个）

| id | 文件 | 一句话 | live 验证状态 |
|---|---|---|---|
| web-session-chat | [web-session-chat.md](./web-session-chat.md) | 空首页 → 草稿会话 → 发消息 | 失败（2026-10-12，main@1a4790a） |
| workspace-management | [workspace-management.md](./workspace-management.md) | `新建工作区`、目录选择、路径冲突 | 失败（2026-10-12，main@1a4790a） |
| session-sidebar | [session-sidebar.md](./session-sidebar.md) | 会话列表、`重命名`、`归档`、状态徽标 | 部分（2026-10-12，main@1a4790a；归档不可撤销） |
| composer-attachments | [composer-attachments.md](./composer-attachments.md) | 图片/文件上传、`本机文件`、下载附件 | 失败（2026-10-12，main@1a4790a） |
| tool-authorization | [tool-authorization.md](./tool-authorization.md) | `等待授权`、批准/拒绝、`自动放行写文件等工具`、确认请求 | 失败（2026-10-12，main@1a4790a；正常批准/拒绝通过） |
| tool-coding-round | [tool-coding-round.md](./tool-coding-round.md) | `read_file`/`apply_patch`/`execute_command` 等文件与命令工具 | 通过（2026-10-12，main@1a4790a） |
| file-rewind | [file-rewind.md](./file-rewind.md) | `从这一步之后重开` → `会话和文件一起回滚` / `只回滚会话` | 部分（2026-10-12，main@1a4790a；主路径通过；人机混改失败） |
| edit-and-branch | [edit-and-branch.md](./edit-and-branch.md) | `编辑消息` → `改写并执行`；`从这一轮之后建立新会话` | 通过（2026-10-12，main@1a4790a；改写后模型回复偏题，待查） |
| run-control | [run-control.md](./run-control.md) | `暂停`/`继续`、`中断`、`重试`、`退回编辑栏` | 部分（2026-10-12，main@1a4790a；中断能结束进程；计划状态残留） |
| subagents | [subagents.md](./subagents.md) | `delegate`、`子 Agent` 面板、`交回结论`、`compare_subagent`/`merge_subagent` | 通过（2026-10-12，main@1a4790a） |
| work-plan | [work-plan.md](./work-plan.md) | 计划生命周期：创建、状态标记、更新、回收（结束/中断/回滚/compact/新会话/子 Agent）、完成归档 | 失败（2026-10-12，main@1a4790a） |
| work-plan-and-usage | [work-plan-and-usage.md](./work-plan-and-usage.md) | 轮次耗时、`上下文用量`、compact（计划部分指向 work-plan） | 通过（2026-10-12，main@1a4790a；只经 API 核对，UI 未验） |
| model-settings | [model-settings.md](./model-settings.md) | `模型配置` 页、`测试`、`保存配置`、会话切换模型 | 通过（2026-10-12，main@1a4790a） |
| agent-workspace-restore | [agent-workspace-restore.md](./agent-workspace-restore.md) | 模型调用 `restore_workspace` 撤回文件变化 | 失败（2026-10-12，main@1a4790a） |
| scheduling | [scheduling.md](./scheduling.md) | `schedule_once` / `cancel_schedule` 定时唤醒 | 通过（2026-10-12，main@1a4790a；重启后持久性未测） |
| extensions | [extensions.md](./extensions.md) | MCP / Skill / CLI 的安装、启用、测试（对话内与 `/mcp` `/skill`） | 未跑（2026-10-12，main@1a4790a；本轮时间不足） |
| realtime-events | [realtime-events.md](./realtime-events.md) | `/api/events` SSE、连接状态、多标签页同步 | 部分（2026-10-12，main@1a4790a；流式通过；断线重连未测） |
| layout-appearance | [layout-appearance.md](./layout-appearance.md) | `外观`、侧栏宽度、窄屏会话列表 | 未跑（2026-10-12，main@1a4790a；纯界面，UI 未验） |
| console-chat | [console-chat.md](./console-chat.md) | 终端入口 `console_chat.py`、`/new`、`/resume` | 跳过（2026-10-12，main@1a4790a；需真 TTY） |
| acp-channel | [acp-channel.md](./acp-channel.md) | `acp_chat.py` ACP 入口 | 跳过（2026-10-12，main@1a4790a；需 ACP 客户端） |
| subagents-parallel | [subagents-parallel.md](./subagents-parallel.md) | 并行多个 `delegate`、副本隔离、并发合并冲突、收回、父会话控制与计量 | 失败（2026-10-12，main@1a4790a） |
| crash-recovery | [crash-recovery.md](./crash-recovery.md) | kill -9 Web/Worker/沙箱、断网、模型 429/5xx/超时/断流/无效 key 后的恢复与去重 | 部分（2026-10-12，main@1a4790a；kill -9 后工具不重复执行（通过）） |
| rewind-edge-cases | [rewind-edge-cases.md](./rewind-edge-cases.md) | compact 前后回滚、compact 中回滚、带附件会话的回滚与改写、人机混合修改文件的回滚（A/B 情况） | 部分（2026-10-12，main@1a4790a；compact 前后回滚通过） |
| setup-and-sandbox-install | [setup-and-sandbox-install.md](./setup-and-sandbox-install.md) | `setup.sh`/`setup.ps1`、模型向导、`build_sandbox.py`、`sandbox_runtime.py`、`dev.sh` | 通过（2026-10-12，main@1a4790a；doctor 与 sandbox 构建通过） |

## 入口 → 地图条目 覆盖表

### Web 路由（`web/src/app/App.tsx`）

| 入口 | 条目 |
|---|---|
| `/` → `DraftRedirect` | web-session-chat |
| `/workspaces/:workspaceId` | web-session-chat、workspace-management |
| `/sessions/:sessionId` → `SessionConversation` | web-session-chat 及所有会话类条目 |
| `/settings/models` → `ModelSettingsPage` | model-settings |
| 其他路径回落 `index.html`（`app.py` 的 `/{path:path}`） | web-session-chat（Gotchas） |

### HTTP / SSE 接口（`redpanda/channels/web/app.py`）

| 接口 | 条目 |
|---|---|
| `GET /api/events` | realtime-events |
| `GET/PUT /api/model-settings`、`POST /api/model-settings/test` | model-settings |
| `GET/PUT /api/sessions/{id}/model` | model-settings |
| `GET /api/sessions`、`GET /api/session-titles`、`GET /api/sessions/{id}` | session-sidebar、web-session-chat |
| `POST /api/sessions` | web-session-chat、session-sidebar |
| `POST /api/sessions/{id}/select` | session-sidebar |
| `POST /api/sessions/{id}/title`、`POST /api/sessions/{id}/archive` | session-sidebar |
| `GET/POST /api/workspaces`、`POST /api/workspaces/select-directory` | workspace-management |
| `POST /api/files/select`、`POST .../attachments`、`POST .../attachments/from-path`、`GET .../attachments/{attachment_id}` | composer-attachments |
| `POST /api/sessions/{id}/inputs` | web-session-chat |
| `POST .../commands/{command_id}/authorize`、`POST .../auto-authorize`、`POST .../control` | tool-authorization |
| `POST .../restarts` | file-rewind、rewind-edge-cases |
| `POST .../forks`、`POST .../branches` | edit-and-branch、rewind-edge-cases（带附件改写） |
| `POST .../paused`、`POST .../cancel`、`POST .../retry` | run-control、crash-recovery（故障后重试） |
| `GET .../subagents/{command_id}` | subagents、subagents-parallel |
| `/assets/*`、`/{path:path}` 静态页 | web-session-chat |

### 模型可调用的工具（Assistant 能力，经对话触发）

| 工具 | 来源 | 条目 |
|---|---|---|
| `read_file` `glob` `grep` `write_file` `apply_patch` `replace_all` `execute_command` `get_changes` | `redpanda/tools/builtin/` | tool-coding-round |
| `restore_workspace` | `redpanda/tools/builtin/workspace_restore.py` | agent-workspace-restore |
| `delegate` `reclaim` `report` `compare_subagent` `merge_subagent` | `redpanda/assistant/subagent/subagent.py`、`tools/builtin/subagent_workspace.py` | subagents、subagents-parallel |
| `read_image` | `redpanda/assistant/attachments.py` | rewind-edge-cases、composer-attachments |
| `update_plan` | `redpanda/assistant/work_plan.py` | work-plan |
| `model_usage` `find_history` | `redpanda/assistant/compact/core.py` | work-plan-and-usage |
| `schedule_once` `cancel_schedule` | `redpanda/automation/tool.py` | scheduling |
| `load_management_tools` `load_toolset` `list_mcp_servers` `test_mcp_server` `propose_mcp_*` `load_skill` `read_skill_resource` `list_installed_skills` `test_installed_skill` `skill_help` `propose_skill_*` `load_cli` `list_installed_clis` `inspect_cli` `test_cli` `propose_cli_*` | `redpanda/mcp/`、`redpanda/skills/`、`redpanda/cli/`、`redpanda/assistant/management.py` | extensions |
| `deliver`、`read_content` | `redpanda/assistant/delivery.py`、`content.py` | **未单独覆盖**：内部投递/读取附件内容，经 composer-attachments 与 web-session-chat 间接走到 |

### 命令行与脚本

| 入口 | 条目 |
|---|---|
| `web_chat.py [--dev] [--host] [--port] [--workspace]` | web-session-chat（`--workspace` 见 workspace-management Gotchas） |
| `console_chat.py [--workspace]`，会内 `/new` `/resume <id>` `/mcp ...` `/skill ...` | console-chat、extensions |
| `acp_chat.py [--workspace]` | acp-channel |
| `scripts/setup.sh`、`scripts/setup.ps1`、`scripts/setup.py [--no-configure] [--check]` | setup-and-sandbox-install |
| `python -m redpanda.initialize --configure / --if-needed` | setup-and-sandbox-install |
| `scripts/build_sandbox.py [--check] [--debug] [--offline] ...` | setup-and-sandbox-install |
| `scripts/sandbox_runtime.py install [--replace] [--with-runtime]` | setup-and-sandbox-install |
| `scripts/sandbox_runtime.py package --platform ... --output ...` | **不覆盖**：发布打包，面向维护者，非用户入口 |
| `scripts/install_winfsp.ps1` | setup-and-sandbox-install（仅 Windows，未 live） |
| `scripts/dev.sh`、`scripts/dev.ps1` | setup-and-sandbox-install |

### 非入口类场景（故障与边界）

| 场景 | 源码 | 条目 |
|---|---|---|
| Worker 失败 → SSE `session_failed` | `redpanda/channels/web/app.py` `report_worker_failures`、`assistant/host/supervisor.py` | crash-recovery |
| 未完成命令 `CommandPhase.UNKNOWN` / `NO_RESULT_YET` | `runtime/state.py`、`assistant/context/projection.py`、`assistant/conversations.py` | crash-recovery |
| 模型错误分类与重试 | `thinllm/chat_completions.py`、`assistant/failures.py` | crash-recovery |
| 输入去重 `delivery_id` | `runtime/journal/sqlite.py` | crash-recovery |
| 残留 FUSE 挂载清理 | `native/sandbox/src/main.rs` `clear_stale_mount` | crash-recovery |
| 子 Agent 重启接管与中断回传 | `assistant/subagent/subagent.py` `rehydrate`、`record_interrupted_return` | subagents-parallel、crash-recovery |
| 人机混合修改回滚（preserve/original、`restore_summary`、carryover/rescue 事实） | `native/sandbox/src/restore.rs`、`restore/namespace.rs`、`sandbox/files/operations.py`、`assistant/workspace_versions.py` | rewind-edge-cases、agent-workspace-restore |
| 回滚分叉、compact 窗口继承、附件继承抽屉 | `assistant/host/session_store.py`、`assistant/compact/core.py`、`assistant/asset_locations.py` | rewind-edge-cases |

### 有意不覆盖

- `thinllm/`：底层 LLM 适配库，不是用户入口；经 model-settings 的 `测试` 间接覆盖。
- `native/vfs`、`native/sandbox` 的 Rust 内部 API：由 `cargo test` 覆盖，用户只经文件工具与回滚感知，见 tool-coding-round、file-rewind。
- `redpanda/runtime/journal` 的内部恢复逻辑：用户不可直接触发；判据里通过 `GET /api/sessions/{id}` 读 journal 投影。
- macOS 文件视图：源码未实现，始终跳过。


## 2026-10-12 live 实跑
证据根目录：`/workspace/verify-run-20261012`（下文路径相对于它），完整报告见该目录 `REPORT.md`。
