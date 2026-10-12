# 扩展能力：MCP、Skill、CLI

助手可按需加载和管理外部能力；安装、更新、删除等都以 `propose_*` 形式请用户确认。终端入口另有 `/mcp`、`/skill` 命令。来源：`redpanda/mcp/`、`redpanda/skills/`、`redpanda/cli/`、`redpanda/assistant/management.py`、`redpanda/assistant/toolsets.py`。

## Sub-features

- `load`：`load_management_tools`、`load_toolset`、`load_skill`、`read_skill_resource`、`load_cli`。
- `inspect`：`list_mcp_servers`、`test_mcp_server`、`list_installed_skills`、`test_installed_skill`、`skill_help`、`list_installed_clis`、`inspect_cli`、`test_cli`。
- `propose`：`propose_mcp_install|update|remove|recovery`、`propose_skill_install|update|uninstall|set_enabled`、`propose_cli_install|update|uninstall|repair`，界面显示 `等待确认`，经 `POST .../control` 决定。
- `console-commands`：`/mcp list [--runtime]`、`/mcp upsert <json>`、`/mcp enable|disable|remove|test|retry <server_id>`、`/mcp read-resource <server_id> <uri>`、`/mcp get-prompt <server_id> <name> [json-args]`、`/skill install [local|url|github] <locator> [ref]`、`/skill update <id> ...`、`/skill set_enabled <id> <true|false>`、`/skill <action> <id>`。

## How to get to it (user POV)

- Web：发 `请列出已安装的 MCP 服务器和 Skill。`；安装时发 `请从本地目录 <路径> 安装这个 skill。`
- 终端：见 console-chat，直接输入 `/mcp list`。

## Driving it with the browser

- **列出**：(a) 回复与 `$REDPANDA_HOME/mcp/`、`skills/`、`clis/` 实际内容一致（新 home 应为空）。(c) 列出不写文件。
- **安装 Skill（拒绝）**：准备一个最小 `SKILL.md` 目录，让助手安装 → 出现 `等待确认` → 拒绝。(a) `skills/` 不变。(d) 再次请求后批准 → `skills/` 出现该 skill，`list_installed_skills` 含它；`test_installed_skill` 通过。
- **MCP upsert 校验**（终端）：(d) `/mcp upsert {}` → `/mcp upsert 缺少字段: ...`；多余字段 → `包含未知字段`；非法 JSON → `需要合法 JSON`。`mcp/` 不变。
- **MCP 命令解释器拦截**：(d) 让助手安装 transport 命令为 `bash`/`sh`/`powershell` 等的 MCP（`redpanda/mcp/approval.py` 中的列表）→ 应被拒绝或明确警告，不得直接写入。
- **密钥**：安装带密钥的 MCP 后，(c) 回复、journal、`GET /api/sessions/<id>` 里不出现明文密钥（`redpanda/mcp/secrets.py`）。

## Gotchas

- Web 端没有 `/mcp` `/skill` 斜杠命令，只能经对话由助手调用工具；斜杠命令只在终端入口生效。
- github/url 来源需要网络，离线时只验 local。

## Live status
- 未跑（2026-10-12，main@1a4790a）：本轮时间不足
