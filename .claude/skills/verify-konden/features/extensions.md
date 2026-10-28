# 扩展能力：MCP、Skill、CLI

助手可按需加载和管理外部能力；安装、更新、删除等都以 `propose_*` 形式请用户确认。终端入口另有 `/mcp`、`/skill` 命令（没有 `/cli` 命令）。来源：`konden/mcp/`（`approval.py`、`console.py`、`application.py`）、`konden/skills/`（`approval.py`、`console.py`）、`konden/cli/`（`approval.py`、`management_tools.py`、`models.py`、`runtime.py`）、`konden/assistant/management.py`、`konden/assistant/toolsets.py`、`konden/assistant/sessions.py`（终端的 yes/no 回答）、`web/src/features/conversation/PendingActions.tsx`（输入框上方待处理条）。

## Sub-features

- `capability-inventory`：侧栏底部 `能力` 图标直接打开 `/capabilities`，不经过设置。Skill、MCP、CLI 页签展示数量、搜索与可展开的清单；产品内置项与用户安装项一起呈现，停用项仍可查。用户安装的 Skill、MCP 可启停，CLI 没有启停开关。MCP 检测结果与启用状态分开；CLI 体检、版本均为已有记录。页面不连接外部 MCP、不运行 CLI，不展示 MCP 凭据与连接配置。

- `bundled-browsers`：空安装登记的 Bot 默认可见 `mcp:builtin-chrome-devtools` 与 `mcp:builtin-playwright`；通过 `load_toolset` 按需加载，具体工具下一步才可调用，不产生安装提案或安装记录。每个连接使用独立的无头 Chrome，会话关闭后不保留登录状态；只读子 Agent 的能力目录排除两者。内置能力随产品更新，安装管理清单仍只列用户安装的 Server。

- `deepseek-web-search`：只需在当前 home 的 `connections.json` 填写 `deepseek.api_key`，只读 `mcp:builtin-deepseek` 自动可用，无安装登记或密钥副本。能力清单列为产品内置，启用状态跟随密钥是否填写，没有独立开关；Bot 按需加载 `web_search`、`web_fetch`，来源与错误如实返回。修改密钥后下一次请求生效，清空后从模型目录移除。搜索优先用户配置的搜索 MCP，其次可用的 DeepSeek；浏览器按需补充动态内容、交互与视觉证据，也能在检索服务不可用时搜索。准备环境不探测外部检索服务。

- `bundled-skills`：空数据根无需安装即可在能力目录发现 `builtin:coding` 与 `builtin:git-collaboration`，正文经 `load_skill` 读取，不注入系统提示，也不作为用户安装记录。工作目录的 `.claude/skills/` 在无 Git、无登记项目的 Bot 中同样可发现为 `project:` Skill。验证时让普通 Bot 按目录自定义 Skill 交付文件，并核对调用与真实文件。

- `load`：`load_management_tools`、`load_toolset`、`load_skill`、`read_skill_resource`、`load_cli`。
- `inspect`：`list_mcp_servers`、`test_mcp_server`、`list_installed_skills`、`test_installed_skill`、`list_installed_clis`、`inspect_cli`、`test_cli`。
- `propose`：`propose_mcp_install|update|remove|recovery`、`propose_skill_install|update|uninstall|set_enabled`、`propose_cli_install|update|uninstall|repair`。Web 在输入框上方的待处理条（区域 `待处理`）列出该请求，按钮 `取消` / `确认`，经 `POST .../control` 决定；终端里回答 `yes`/`y`（批准）或 `no`/`n`（拒绝）。
- `mcp-stdio-guard`：`propose_mcp_install` / `propose_mcp_update` 的 stdio `command` 文件名（不区分大小写）为 `bash`、`cmd`、`cmd.exe`、`powershell`、`powershell.exe`、`pwsh`、`sh`、`zsh` 时直接校验失败 `MCP stdio 不接受 Shell 解释器作为 command`，不进入确认。
- `skill-source`：来源 `local` / `url` / `github`；`local` 的 locator 必须是本机绝对目录，否则 `local locator 必须是本机绝对目录`；`requested_ref` 只用于 `github`，其他来源传了即 `requested_ref 仅适用于 github 来源`。
- `cli-register`：`propose_cli_install` 的 `locator` 登记本机已有程序；`repo` 从 GitHub Release 下载，两者二选一。Release 提案展示确定的 tag，下载在批准后发生；修复重新下载同一 tag，卸载审批明确会删除下载的程序与安装目录。本机手工安装的软件在移除登记后保留。`name` 须匹配 `^[a-z0-9][a-z0-9-]{0,63}$`，不合规返回 `code: CLI_INVALID`；已登记返回 `CLI_ALREADY_INSTALLED`；找不到程序或下载失败返回 `CLI_SOURCE_ERROR`。登记后立即可用，没有启用/停用开关。
  - `control-startup-recovery`：重启会继续已提交的控制准备、已批准但尚未开始的操作；提案仍待批准时不执行，已开始而结果未知时不自动重试，人已停止执行的会话不自动执行。连续重启不能重复已完成的操作。
- `console-commands`：
  - `/mcp list [--runtime]`、`/mcp upsert <json>`、`/mcp enable|disable|remove|test|retry <id>`、`/mcp resources|resource-templates|prompts <id> [--cursor TOKEN]`、`/mcp read-resource <id> <uri>`、`/mcp get-prompt <id> <name> [json-args]`、`/mcp help`（`/mcp` 不带子命令同样显示帮助）。
  - `/skill list [id]`、`/skill install [local|url|github] <locator> [ref]`、`/skill test <id>`、`/skill set_enabled <id> <true|false>`、`/skill uninstall <id>`、`/skill update <id> [[local|url|github] <locator> [ref]]`、`/skill help`。

## How to get to it (user POV)

- Web 清单：点击侧栏底部 `能力`；窄屏先打开 `打开 Agent 名册`。分类为 `Skill`、`MCP`、`CLI`，搜索为 `搜索 Skill` 等，`查看 <名字>` 展开详情。
- Web：发 `请列出已安装的 MCP 服务器、Skill 和 CLI。`；安装时发 `请从本地目录 <绝对路径> 安装这个 skill。`；登记 CLI 发 `请把本机的 git 登记为 CLI，名称 git。`
- 终端：见 console-chat，直接输入 `/mcp list`、`/skill list`。

## Driving it with the browser

- **能力清单**：从侧栏打开 `能力`，切换三个页签、搜索名称或用途、展开来源与版本；再对用户安装项操作 `启用 <名字>` / `停用 <名字>`。
  - (a) 与 `GET /api/capabilities` 对照，包含产品内置与停用安装，CLI 仅为系统已登记项。
  - (b) 启停经 `PUT /api/capabilities/{skills|mcp}/{id}/enabled` 保存，后续 GET 和刷新保持；MCP 仅开启时仍显示原检测结果，不能变成「连接正常」。
  - (c) 内置项不出现开关，CLI 不出现开关，列表读取不连接服务或执行命令，也不泄露 MCP 凭据。
  - (d) 无匹配结果与读取失败分开显示；读取失败有 `重试`。1440×900 与 390×844 的明暗主题均可读，220px 侧栏底部允许换行，自动放行标签不竖排。

- **默认浏览器**：按 setup-and-launch 准备环境，在空 home 从页面创建无项目 Bot，让它分别用 Chrome DevTools 与 Playwright 打开本次隔离 Web 的首页、读取标题并截图。
  - (a) 会话初始能力目录含两项内置 Toolset；Journal 留下各自的加载与真实调用结果，用户安装登记仍为空。
  - (b) 页面显示两项调用完成与读取的网页标题，无安装待确认条；截图结果可查看。
  - (c) 另一个 Bot 同时使用浏览器时不争用配置、不共享登录状态；只读子 Agent 的目录不含这两项能力。
  - (d) 缺浏览器依赖时加载或调用给出原始连接失败与重新准备产品环境的提示，不建议重新安装一个同名外部 MCP。

- **列出**：
  - (a) 回复与 `$KONDEN_HOME/mcp/`、`skills/`、`clis/` 实际内容一致（新 home 应为空）。
  - (b) 模型调用的是 `list_mcp_servers` / `list_installed_skills` / `list_installed_clis`；终端 `/mcp list` 在空 home 打印 `尚未注册任何 MCP Server。`。
  - (c) 列出不写文件。
  - (d) `/mcp list --foo` → `/mcp list 只接受可选参数 --runtime`。
- **DeepSeek 网页检索**：在隔离 home 只填写官方 Key，从 `能力 → MCP` 展开 `查看 DeepSeek Web Search`，核对产品内置、只读与启用状态，页面与 `GET /api/capabilities` 均不含密钥。创建无项目 Bot，发送 `请联网查找 DeepSeek 网页搜索与抓取 API 的官方文档，读取其中一个官方页面的原文，并给出来源链接。`，核对会话中的真实 Search、Fetch 调用与带来源的回答，确认安装登记与 SecretStore 没有新增项；运行时清空再填回密钥，能力清单随之更新，后续请求仍可用。重启隔离 Web 后回答与调用仍可恢复。认证失败、余额不足、限流与抓取失败由接口边界测试保护；本项没有逐一触发真实服务错误。
- **检索与浏览器组合**：先发送普通文字查证任务，核对真实搜索、正文与来源；再要求补充原始页面中有助于理解的视觉信息，核对即使检索成功仍能使用浏览器，并把真实图片交付到对话。已有证据足够时要求整理成图示，核对无需再次浏览即可使用已有数据。对照执行过程、`GET /api/sessions/<id>` 与图片附件，文字快照不算实际看图，模型没有查看过的视觉细节不得当作已核实事实。分别记录模型自主选择与用户明确要求的路径，不把单次实测写成必然行为。
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
- **密钥**：安装带密钥的 MCP 后，(c) 回复、journal、`GET /api/sessions/<id>` 里不出现明文密钥（`konden/mcp/secrets.py`）。

## Gotchas

2026-10-28 CLI Release 修复增量：Windows 隔离数据根，经生产控制提案与执行 handler 实测 ripgrep 15.2.0 下载、CertUtil sha256 校验、体检、删除程序后的同 tag 修复及卸载。真实页面从侧栏打开能力、CLI，展开版本和通过的体检，再刷新确认卸载后 CLI0，与 GET 和磁盘一致。证据留在本地 `.tools/review200/live/evidence/`。Doctor `live_model_ready=false`，未驱动模型对话中的审批按钮；macOS/Linux 未做真实下载，失败保留旧程序与并发同名安装由进程内及独立进程回归保护。

- 2026-10-28 检索与图文组合增量：Windows 隔离 Web、真实 DeepSeek Flash，无项目 Bot 的普通查证任务自主使用 Search / Fetch；随后用户要求视觉补充时，真实 Playwright 打开同一官方来源并交付可预览 PNG，没有重复检索。再要求按已有资料画图时，直接渲染 Mermaid，没有追加工具调用。证据 `.tools/verification/web-research-multimodal/evidence/`；后两项为用户明确要求的路径，未验证不同模型的自主视觉补充、真实 Tavily、登录站点或完整性能与费用对比。

2026-10-26 能力清单增量：Windows 隔离 Web，经侧栏真实打开并操作 Skill/MCP/CLI 清单；停用项仍可查，启停持久保存且 MCP 原检测结果不变，断网错误可重试恢复。1440×900、390×844 明暗主题与 220px 侧栏通过。证据留在本地 `.tools/capabilities-evidence/`；无默认模型，未发模型消息，未重验外部服务连接、模型侧安装与完整控制流程。

- Web 端没有 `/mcp` `/skill` 斜杠命令，只能经对话由助手调用工具；斜杠命令只在终端入口生效。CLI 没有斜杠命令，只能经对话。
- `skill_help` 工具已删除，不要再按它验证。
- Shell 解释器拦截只在 `propose_mcp_install` / `propose_mcp_update` 的参数校验里；终端 `/mcp upsert` 走 `McpApplicationService.upsert_server`，不经过这条校验，能直接登记 `bash` 作 command（2026-10-18 源码核对）。验证时不要用 `/mcp upsert` 判定拦截。
- 已知产品缺口（2026-10-18 源码核对）：`/mcp help` 列出 `/mcp reload`，但分发里没有 `reload` 分支，输入后得到 `未知 /mcp 子命令: reload`。正确预期是帮助里列出的子命令都能执行；验证时记为失败而不是跳过。
- github/url 来源需要网络，离线时只验 local。
- 历史验证：2026-10-12（main@1a4790a）未跑。2026-10-18 按源码改写，未 live 复跑。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：真实加载 MCP/Skill/CLI 管理工具并列空目录；propose_cli_install 登记本机 git 的控制提案和页面确认通过。Skill/MCP 安装与取消路径未覆盖。

- 2026-10-20 编码 Skill 增量：隔离空安装登记可发现内置编码与 Git Skill；真实无 Git Bot 从个人目录发现 project:file-handoff，并与 builtin:coding 分别加载后交付文件。普通讨论未加载编码 Skill。证据 `.tools/verify-coding-skill/evidence/no-git-coding/`；未重验外部包安装、更新与取消。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：真实加载 MCP/Skill/CLI 管理工具并列出空安装清单；安装、测试、控制提案和移除未重验。

- 2026-10-22 启动恢复增量：隔离 Windows Web、Chrome、真实 DeepSeek；消息框发起本机 Git 的 CLI 登记，页面点击确认后，在批准已写入、执行尚未开始处注入 Worker 退出。正常入口重启后原操作成功，页面显示登记结果；再次重启 Journal 不变，没有重复执行。控制准备、待批准、暂停与执行结果未知的边界由进程回归核对；未重验其他控制操作的实际安装与更新。证据：`.tools/recovery-fixes/startup-web-evidence/`。

- 2026-10-23 浏览器增量：Windows、隔离空安装登记、无项目 Bot、真实 DeepSeek。页面创建的 Bot 首次即发现两项内置 Toolset，真实打开隔离首页、读取标题与截图；重启后重新加载并把两张 PNG 直接保存至个人目录、交付附件。桌面 1440×900 结果与 GET、文件头一致。另以四个真实 MCP 连接核对独立页面状态与截图目录；只读目录过滤、断线结果未知与外部安装 ID 冲突由回归守约。证据 `.tools/browser-evidence/`；Linux 与需要登录的外部网站未实跑。

以下两条为已移除能力的历史记录：内置 Exa 与其准备检查已移除，当前检索顺序以本页的 `deepseek-web-search` 和内置检索 Skill 为准。

- 2026-10-23 默认检索增量：Windows、隔离空安装登记、无项目 Bot、真实 DeepSeek。页面发起中文任务，Bot 加载内置检索 Skill，经 Exa 搜索并读取官网正文；浏览器演练要求本轮不用 Exa，Bot 经 Bing 调整查询、打开官网核对并交付截图。另以产品 MCP 客户端核对技术、场馆与法条三类中文搜索和批量正文读取；已知限流与空返回由回归守约。证据 `.tools/exa-evidence/`；免费额度耗尽后的真实 429、Linux 真实浏览器、不同地区网络未验证。

- 2026-10-23 检索优先级增量：Windows 隔离 Web、真实 DeepSeek、普通检索请求均未指定服务。两位新 Bot 均先使用用户已配置并启用的可控检索 MCP；一例因正文不足再用真实 Exa，另一例收到模拟 HTTP 429 后转用真实 Exa 并读取官网完成任务。证据 `.tools/priority-evidence/`；本轮未使用真实 Tavily 账号，未重跑浏览器末级兜底（上一增量已有实测），不代表不同模型均必然按同一路径执行。
