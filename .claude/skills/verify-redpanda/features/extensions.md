# 扩展能力：MCP、Skill、CLI

助手可按需加载和管理外部能力；安装、更新、删除等都以 `propose_*` 形式请用户确认。终端入口另有 `/mcp`、`/skill` 命令（没有 `/cli` 命令）。来源：`redpanda/mcp/`（`approval.py`、`console.py`、`application.py`）、`redpanda/skills/`（`approval.py`、`console.py`）、`redpanda/cli/`（`approval.py`、`management_tools.py`、`models.py`、`runtime.py`）、`redpanda/assistant/management.py`、`redpanda/assistant/toolsets.py`、`redpanda/assistant/sessions.py`（终端的 yes/no 回答）、`web/src/features/conversation/PendingActions.tsx`（输入框上方待处理条）。

## Sub-features

- `bundled-skills`：空数据根无需安装即可在能力目录发现 `builtin:coding` 与 `builtin:git-collaboration`，正文经 `load_skill` 读取，不注入系统提示，也不作为用户安装记录。工作目录的 `.claude/skills/` 在无 Git、无登记项目的 Bot 中同样可发现为 `project:` Skill。验证时让普通 Bot 按目录自定义 Skill 交付文件，并核对调用与真实文件。

- `load`：`load_management_tools`、`load_toolset`、`load_skill`、`read_skill_resource`、`load_cli`。
- `inspect`：`list_mcp_servers`、`test_mcp_server`、`list_installed_skills`、`test_installed_skill`、`list_installed_clis`、`inspect_cli`、`test_cli`。
- `propose`：`propose_mcp_install|update|remove|recovery`、`propose_skill_install|update|uninstall|set_enabled`、`propose_cli_install|update|uninstall|repair`。Web 在输入框上方的待处理条（区域 `待处理`）列出该请求，按钮 `取消` / `确认`，经 `POST .../control` 决定；终端里回答 `yes`/`y`（批准）或 `no`/`n`（拒绝）。
- `mcp-stdio-guard`：`propose_mcp_install` / `propose_mcp_update` 的 stdio `command` 文件名（不区分大小写）为 `bash`、`cmd`、`cmd.exe`、`powershell`、`powershell.exe`、`pwsh`、`sh`、`zsh` 时直接校验失败 `MCP stdio 不接受 Shell 解释器作为 command`，不进入确认。
- `skill-source`：来源 `local` / `url` / `github`；`local` 的 locator 必须是本机绝对目录，否则 `local locator 必须是本机绝对目录`；`requested_ref` 只用于 `github`，其他来源传了即 `requested_ref 仅适用于 github 来源`。
- `cli-register`：`propose_cli_install` 只登记本机已安装的程序（`name`、`description`、`locator`），`name` 须匹配 `^[a-z0-9][a-z0-9-]{0,63}$`，不合规返回 `code: CLI_INVALID`；已登记返回 `CLI_ALREADY_INSTALLED`；找不到程序返回 `CLI_SOURCE_ERROR`。登记后立即可用，没有启用/停用开关。
- `console-commands`：
  - `/mcp list [--runtime]`、`/mcp upsert <json>`、`/mcp enable|disable|remove|test|retry <id>`、`/mcp resources|resource-templates|prompts <id> [--cursor TOKEN]`、`/mcp read-resource <id> <uri>`、`/mcp get-prompt <id> <name> [json-args]`、`/mcp help`（`/mcp` 不带子命令同样显示帮助）。
  - `/skill list [id]`、`/skill install [local|url|github] <locator> [ref]`、`/skill test <id>`、`/skill set_enabled <id> <true|false>`、`/skill uninstall <id>`、`/skill update <id> [[local|url|github] <locator> [ref]]`、`/skill help`。

## How to get to it (user POV)

- Web：发 `请列出已安装的 MCP 服务器、Skill 和 CLI。`；安装时发 `请从本地目录 <绝对路径> 安装这个 skill。`；登记 CLI 发 `请把本机的 git 登记为 CLI，名称 git。`
- 终端：见 console-chat，直接输入 `/mcp list`、`/skill list`。

## Driving it with the browser

- **列出**：
  - (a) 回复与 `$REDPANDA_HOME/mcp/`、`skills/`、`clis/` 实际内容一致（新 home 应为空）。
  - (b) 模型调用的是 `list_mcp_servers` / `list_installed_skills` / `list_installed_clis`；终端 `/mcp list` 在空 home 打印 `尚未注册任何 MCP Server。`。
  - (c) 列出不写文件。
  - (d) `/mcp list --foo` → `/mcp list 只接受可选参数 --runtime`。
- **安装 Skill（取消后确认）**：准备一个最小 `SKILL.md` 目录，让助手按绝对路径安装 → 待处理条出现 `准备安装 Skill …` 一项 → 点 `取消`。
  - (a) `skills/` 不变。
  - (b) 再次请求后点 `确认` → `skills/` 出现该 skill，`list_installed_skills` 含它；`test_installed_skill` 通过。
  - (c) 取消那次不留下半成品目录。
  - (d) 给相对路径 → 工具校验失败 `local locator 必须是本机绝对目录`，不弹确认；`local` 来源附带 ref → `requested_ref 仅适用于 github 来源`。
- **登记 CLI**：让助手登记本机的 `git`。
  - (a) 确认后 `clis/` 出现登记，`list_installed_clis` 含它，`inspect_cli` 返回解析路径与体检。
  - (b) 待处理条中该项摘要含 `准备登记 CLI`、`解析路径：`、`体检：`。
  - (c) 不下载任何软件，只登记已有程序；`取消` 后 `clis/` 不变。
  - (d) 名称用 `Git_Tool` → `CLI_INVALID`，提示里有 `^[a-z0-9][a-z0-9-]{0,63}$`；不弹确认。
- **MCP upsert 校验**（终端）：
  - (d) `/mcp upsert {}` → `/mcp upsert 缺少字段: id, transport`；多余字段 → `/mcp upsert 包含未知字段: ...`；非法 JSON → `/mcp upsert 需要合法 JSON`；`/mcp upsert` 不带参数 → 打印带示例的用法。以上 `mcp/` 都不变。
- **MCP 命令解释器拦截**（对话）：
  - (d) 让助手安装 stdio `command` 为 `bash`/`sh`/`pwsh`/`cmd.exe` 等的 MCP → 工具校验失败 `MCP stdio 不接受 Shell 解释器作为 command`，待处理条不出现该请求，`mcp/` 不变。
- **未知子命令**：(d) `/skill foo` → `未知 /skill 子命令: foo`；`/skill set_enabled x maybe` → `/skill set_enabled <id> <true|false>`；`/mcp foo` → `未知 /mcp 子命令: foo`。
- **密钥**：安装带密钥的 MCP 后，(c) 回复、journal、`GET /api/sessions/<id>` 里不出现明文密钥（`redpanda/mcp/secrets.py`）。

## Gotchas

- Web 端没有 `/mcp` `/skill` 斜杠命令，只能经对话由助手调用工具；斜杠命令只在终端入口生效。CLI 没有斜杠命令，只能经对话。
- `skill_help` 工具已删除，不要再按它验证。
- Shell 解释器拦截只在 `propose_mcp_install` / `propose_mcp_update` 的参数校验里；终端 `/mcp upsert` 走 `McpApplicationService.upsert_server`，不经过这条校验，能直接登记 `bash` 作 command（2026-10-18 源码核对）。验证时不要用 `/mcp upsert` 判定拦截。
- 已知产品缺口（2026-10-18 源码核对）：`/mcp help` 列出 `/mcp reload`，但分发里没有 `reload` 分支，输入后得到 `未知 /mcp 子命令: reload`。正确预期是帮助里列出的子命令都能执行；验证时记为失败而不是跳过。
- github/url 来源需要网络，离线时只验 local。
- 历史验证：2026-10-12（main@1a4790a）未跑。2026-10-18 按源码改写，未 live 复跑。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：真实加载 MCP/Skill/CLI 管理工具并列空目录；propose_cli_install 登记本机 git 的控制提案和页面确认通过。Skill/MCP 安装与取消路径未覆盖。

- 2026-10-20 编码 Skill 增量：隔离空安装登记可发现内置编码与 Git Skill；真实无 Git Bot 从个人目录发现 project:file-handoff，并与 builtin:coding 分别加载后交付文件。普通讨论未加载编码 Skill。证据 `.tools/verify-coding-skill/evidence/no-git-coding/`；未重验外部包安装、更新与取消。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：真实加载 MCP/Skill/CLI 管理工具并列出空安装清单；安装、测试、控制提案和移除未重验。
