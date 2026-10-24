# 工具编码回合

在侧栏底部打开全局 `自动放行写文件等工具`，让模型改文件、跑命令，时间线依次出现 `read_file`、`apply_patch`、`execute_command`，任务根真实目录里的文件真的变了。工具来源 `konden/tools/builtin/`（`file_read.py`、`file_write.py`、`file_manage.py`、`command_execution.py`、`background_commands.py`），参数校验与 handler 异常转换在 `konden/tools/executor.py`；路径解析与文本读写在 `konden/filesystem/`。所有文件工具与命令直接作用于任务根，没有回退。

## Sub-features

- `coding-tools`：一条消息内跑 `read_file` → `apply_patch` → `execute_command`。
- `command-exit-codes`：`execute_command` 退出码 0 → `code=COMMAND_COMPLETED`；非 0 → `code=COMMAND_EXIT_NONZERO`（`ok=true`，工具状态仍为 `succeeded`/「完成」而非工具失败），`hint` 含「退出码非零」/「不要当作成功」，`data.exit_code` 为实际退出码。
- `command-errors`：`execute_command` 的其他结果码（`command_execution.py`）：
  - `ok:false`：`EMPTY_COMMAND`（「command 不能为空」）、`CWD_NOT_FOUND`（「工作目录不存在: …」）、`CWD_NOT_A_DIRECTORY`（「工作目录不是目录: …」）、`SHELL_NOT_FOUND`（带 `shell`、`executable`）、`COMMAND_START_FAILED`、`COMMAND_IO_FAILED`（「命令管道 I/O 失败，采集结果可能不完整」）。
  - `ok:null`（结果不确定，工具状态投影为 `unknown`/「中断」）：`COMMAND_INTERRUPTED`（「命令被用户停止，可能已部分执行。」）。
- `command-authorization`：Windows/Linux 顶层会话的 `execute_command` 参数为 `cwd`、`command`、`wait_seconds`；不支持后台的平台使用 `cwd`、`command` 并等待结束。每次调用都需要授权（自动放行开启时被自动批准）；工具描述写明直接作用于真实目录、不提供回退，按实际结果核对影响；Git 核对方法由适用 Skill 提供。
- `search-tools`：`glob`、`grep` 在工作区内查找；`glob` 依赖 `fd`/`fdfind`，找不到时返回 `GLOB_NOT_FOUND`（「未找到 fd」）。
- `write-tools`：`write_file`（新建/覆盖）、`replace_all`（替换文件中所有精确匹配文本）。
- `referenced-content`：`read_content` 读取工具结果或历史检索返回的真实 `reference`；可按 `offset`/`limit` 连续读取，或用 `query` 查找字面关键词，按 `next_offset` 续读。来源 `konden/assistant/content.py`。
- `argument-validation`：任一工具参数不符合 schema（如 `wait_seconds` 超出 0–30）→ `ok:false`、`code:VALIDATION_ERROR`、`hint`「按工具 schema 修正参数后重试。」（`executor.py`）。
- `background-commands`：`execute_command(wait_seconds=0)` 立即转后台，等待到点未结束同样返回 `COMMAND_BACKGROUND`；完成事实 `background_command_finished` 只接纳一次并唤醒 Agent。`read_command` / `list_commands` / `stop_command` 查看与停止；成功码 `COMMAND_READ`、`COMMANDS_LISTED`、`COMMAND_STOPPED`，未知身份 `COMMAND_NOT_FOUND`。Windows/Linux 顶层会话注册，子 Agent 不提供；macOS 只有等待结束的前台命令。会话页的 `后台命令 · N 项运行中` 可展开查看状态、时长，`停止命令` 只终止这一条，不停止执行整个 Agent。
- `file-side-effect`：改动真实落盘到 Agent 工作树（见 Gotchas），人自己的 `$SCRATCH_WORKSPACE` 不变。
- `coding-skill`：通用系统提示不含编码与 Git 工作流正文；能力目录提供 `builtin:coding` 与 `builtin:git-collaboration`，模型按任务用 `load_skill` 读取。无 Git 的 Bot 目录也能编码、验证并交付文件；用户指定的 Skill 与流程优先。
- `project-instructions`：项目与普通目录统一由模型在文件工作前用 `read_file` 读取根目录 `AGENTS.md`，没有时读 `CLAUDE.md`，并按需读取相关目录约定和任务文档。全文不再逐轮注入系统提示；约定更新或上下文缺失时重新读取。判据是在约定中指向独立需求文档与验证命令，模型实际读取并执行，而非只看回复是否带固定词。

`live_model_ready` 为 true 才跑本条。

## How to get to it (user POV)

- 打开会话（见 web-session-chat），打开开关 `自动放行写文件等工具`，在 `消息` 里点名要用的工具后 `发送`。

## Driving it with the browser

前置：`$SCRATCH_WORKSPACE` 是已提交 `note.txt`（内容 `verify`）的 Git 仓库，已在名册里用它创建 Agent；Doctor 返回 0。

- **确认开关开启**：现有和新会话共用全局选择，不要无条件点击切换；仅在关闭时点击 `自动放行写文件等工具`。`GET /api/sessions/<id>` 中 `session.auto_authorize` 为 true。
- **发请求**：

```text
请只做这三件事，不要委派，不要安装依赖。
1. 用 read_file 读取 note.txt。
2. 用 apply_patch 把文件内容改成 edited。
3. 用 execute_command 执行 printf '%s\n' edited。
最后回复 CODING_ROUND_DONE。
```

Windows 将第三条命令换成 `Write-Output edited`；不要在 PowerShell 上照抄 `printf`。

- **判据**：
  - (a) `note.txt` 内容为 `edited`；`GET /api/sessions/<id>` 的 `items` 里有依次调用这三个工具的步骤，每个工具 `status` 为 `succeeded`。界面上成功步骤的标题行不显示状态徽标，展开后工具徽标为 `完成`（带勾）。
  - (b) 不出现待处理条（区域 `待处理`）；回复含 `CODING_ROUND_DONE`。
  - **命令退出码**（同会话或后续轮，自动放行开启）：
    1. `execute_command` 跑 `bash -c 'echo boom >&2; exit 2'`→ journal/`outcome.value` 中 `ok=true`、`code=COMMAND_EXIT_NONZERO`、`data.exit_code=2`、`hint` 含「退出码非零」或「不要当作成功」；该工具 `status` 为 `succeeded`（「完成」），不是工具失败。
    2. 再跑 `printf 'ok\n'`（或 `true`）→ `code=COMMAND_COMPLETED`、`data.exit_code=0`。
    Windows 上用 `cmd /c exit 2` 核对原生退出码为 2，再用 `Write-Output ok` 核对成功；`Write-Error "failed"` 可作纯 PowerShell 失败对照。
  - (c) 工作区里除 `note.txt` 外没有新增/修改文件（在 Agent 工作树里对比运行前后的 `git status --porcelain`）；`$SCRATCH_WORKSPACE` 本身不变；`$KONDEN_HOME` 不出现在工作区内。
  - (d) 错误路径①：预置一个在所有允许读取目录之外的哨兵文件，让模型 `read_file` 读取 → 非完全访问工作区应返回工具错误，文件内容不得出现在回复里。同事工作树是允许只读的目录，不能拿它作越界判据。错误路径②：`apply_patch` 打一个上下文不匹配的补丁 → 工具报错，`note.txt` 不变。错误路径③：让模型 `execute_command` 用 `cwd` 指向不存在的子目录 → `CWD_NOT_FOUND`；`wait_seconds=1` 跑 `sleep 5`（Windows 用 `Start-Sleep -Seconds 5`）→ `COMMAND_BACKGROUND`，进程继续运行，稍后收到完成事实。
- **错误路径（工具结果，不打崩会话）**：`glob` 的 pattern 为 `""` / `"."` / 仅空白时返回工具结果 `ok:false`、`code:EMPTY_PATTERN`，会话与 Worker 继续存活。参数不合 schema → `VALIDATION_ERROR`。handler 抛出的普通 `Exception`（如未预期 `ValueError`）由 `ToolsExecutor._execute_spec` 转为工具结果 `ok:false`、`code:TOOL_HANDLER_ERROR`（`data.exception_type` 为异常类名），**不**升级为调度器/Worker failure；`asyncio.CancelledError` / `KeyboardInterrupt` / `SystemExit` 等 `BaseException` 仍上抛。
- **搜索/写入**：让模型 `glob` 找 `*.txt`、`grep` 找 `edited`、`write_file` 新建 `new.txt` 写 `hi`、`replace_all` 把 `hi` 换成 `yo`。(a) `new.txt` 为 `yo`。(d) 实例 PATH 里没有 `fd`/`fdfind` 时 `glob` → `GLOB_NOT_FOUND`，记录为环境问题而非功能通过。
- **结果引用**：某个工具或历史检索实际返回 `data.reference` 后，请模型用 `read_content` 先连续读取，再用 `query` 找一个已知标识。(a) `CONTENT_READ` 的片段与原文一致，续读使用真实 `next_offset`。(b) 时间线出现 `read_content` 调用。(c) 读取不修改源文件。(d) 合法格式但不存在的引用 → `CONTENT_NOT_FOUND`；偏移超出原文 → `OFFSET_OUT_OF_RANGE`。没有取得真实引用时记录未覆盖，不编造成功路径。
- **命令授权**：在侧栏**全局关闭**自动放行后，在会话里让模型 `execute_command` 跑一个只读命令（如 `git status`）→ (b) 待处理条出现该调用（自动放行开启时所有待授权调用都会被自动批准，看不到这一步）；(a) 批准后才执行。(c) 不存在按命令文本放行的情况。(d) 拒绝后命令不执行，模型收到「用户拒绝」。
- **后台接管与响应**：从页面请求 `execute_command` 跑长命令，`wait_seconds=0`，接管后用普通回复回执并结束本轮；命令运行中另发消息，要求普通回复包含标识。(a) `GET /api/sessions/<id>/commands` 仍为 running，后台列表展示时长，新消息已回复。(b) 启动须批准，读取与停止不另授权。(c) 页面 `停止命令` 后状态 stopped，完成事实恰好一条并唤醒 Agent；只有主动停止，没有自动重跑。(d) 再停同一条不再产生完成事实。短命令用 `wait_seconds=1` 等待较长于一秒的操作，确认到点转后台且自然结束后主动通知；核对完成后模型的普通回复直接可见，无需调用交付工具。
- **证据**：三个工具名可见时快照+截图；会话 JSON 与 `note.txt` 副本存到 `$EVIDENCE_DIR/tool-coding-round/`。
- **关开关的反向路径**：新开会话，先关闭自动放行，发 `请用 apply_patch 把 note.txt 改成 edited，然后停止。`，在待处理条点 `允许 apply_patch`（详见 tool-authorization）；`read_file` 是只读工具，不会进入待处理。

## Gotchas

- Web 会话即名册里的 Agent，任务根是它自己的 worktree（`$KONDEN_HOME/state/coding_bot/<sha256(workspace_id) 前 20 位>/<sha256(session_id) 前 20 位>/`），不是 `$SCRATCH_WORKSPACE` 本身；判据里的「磁盘文件」读 Agent 工作树。Agent 通过命令主动暂存/提交。落地与同步（`land_branch`、`sync_branch`）见 [land-and-sync.md](./land-and-sync.md)。Step 收口不自动暂存或提交。

- 非零退出不是工具失败：`ok` 仍为 true，`code` 为 `COMMAND_EXIT_NONZERO`（不是 `COMMAND_COMPLETED`）；模型应读 hint/stderr，不要把非零当作成功。
- `ok:null` 的结果 `COMMAND_INTERRUPTED` 在时间线是「中断」，不是「失败」；不要按失败判。
- 所有写入都不可回退；撤销靠 Git。
- 开关按会话保存，新会话默认开启；授权流程验证须先显式关闭。
- 未配置供应商时 `发送` 是禁用的，不要直接 POST 输入。
- 后台命令工具在 macOS 不注册，前台调用等待到结束。
- `pytest -m process` 走的是同一批工具但不经 Web，只能记为进程测试。
- 模型可能拒绝或换工具；通过条件是三个工具名按顺序出现且 `note.txt` 为 `edited`。
- 约定文件通过普通文件工具读取；不能再用“下一轮系统提示自动包含新内容”作为判据。检查实际读取结果与后续任务行为。

- 2026-10-20（Windows Web + DeepSeek，隔离实例，普通回复改版前）：`wait_seconds=0` 接管后，命令仍运行时新消息得到回复，页面计时 0.8 秒；页面停止后状态 stopped，并主动报告。`wait_seconds=1` 的 5 秒命令自动转后台，自然完成后报告 exit_code=0 和完整短输出。Journal 确认两条接管分别只有一条完成事实，无重跑。Linux、macOS 未经 Web 实测；当时漏发交付工具的问题已随普通回复直接展示改版移除，当前回复规则见 reply-delivery。
- 2026-10-18 改写：文件回退移除后，删除 `file_access`/`workspace_effect`、`get_changes`、`COMMAND_CONSTRAINT_FAILED` 与原生依赖相关判据；命令一律授权。未 live 复跑。
- 2026-10-18 源码核对（未 live）：补充 `execute_command` 其余结果码、`GLOB_NOT_FOUND`、`VALIDATION_ERROR`、进程工具结果码与平台范围、项目说明在 `AGENTS.md` 不可读时改读 `CLAUDE.md`；均未驱动验证。`file_access` 与 `get_changes` 已不在判据里。
- 2026-10-16（pr25-verify merge @e701ef5，Web+DeepSeek）：coding-tools 通过；`bash -c 'exit 2'` → `COMMAND_EXIT_NONZERO` ok=true exit_code=2；`printf ok` → `COMMAND_COMPLETED` exit_code=0
- 2026-10-16（main@e191eab，仅 API，界面未验证）：`read_file`→`apply_patch`→`execute_command` 落盘；`start_process`/`read_process`/`list_processes`/`stop_process` 全部成功，停止后端口释放
- 通过（2026-10-12，main@1a4790a）
- PR #22（2026-10-16）：`EMPTY_PATTERN` 与 `TOOL_HANDLER_ERROR` 路径经 ToolsExecutor 直测 + Web live（glob `.`）通过；会话未崩。
- 历史产品缺口（2026-10-18 Windows live）：原生非零退出码被折叠成 1。2026-10-20 源码核对：`konden/execution/local/powershell.py` 已传递原生 `$LASTEXITCODE`，`tests/tools/test_command_execution.py` 的退出码对照通过；下方维护实测确认 Web 路径返回 2，不再豁免具体退出码判据。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：read_file→apply_patch→execute_command 通过，note.txt 只改在 Ada 工作树；原生 cmd /c exit 2 得到 COMMAND_EXIT_NONZERO、exit_code=2、ok=true。后续子任务审阅真实使用 find_history/read_content；后台接管、运行中新消息回复、自然完成通过，未穷尽错误码。

- 2026-10-20 编码 Skill 增量：隔离 Web + deepseek-flash，普通讨论未加载编码 Skill；无 Git Bot 按需加载 builtin:coding 和 project:file-handoff，读取 AGENTS.md 与独立需求文档、修复函数、执行 unittest（2 个通过）、生成交接文件，用户原文件保留且无 Git 操作。项目 Bot 在实施任务与集成任务中分别加载编码、Git Skill；首轮提议未修改文件。证据 `.tools/verify-coding-skill/evidence/`。未复验所有命令错误码、压缩后重读与子任务实机路径。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：真实 read_file/apply_patch/write_file/execute_command 通过，文件只改在挂载项目内；后台命令与非零退出未重验。
