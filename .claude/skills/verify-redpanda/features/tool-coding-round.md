# 工具编码回合

在会话里打开 `自动放行写文件等工具`，让模型改文件、跑命令，时间线依次出现 `read_file`、`apply_patch`、`execute_command`，任务根真实目录里的文件真的变了。工具来源 `redpanda/tools/builtin/`（`file_read.py`、`file_write.py`、`file_manage.py`、`command_execution.py`、`process_management.py`），参数校验与 handler 异常转换在 `redpanda/tools/executor.py`；路径解析与文本读写在 `redpanda/filesystem/`。所有文件工具与命令直接作用于任务根，没有回退。

## Sub-features

- `coding-tools`：一条消息内跑 `read_file` → `apply_patch` → `execute_command`。
- `command-exit-codes`：`execute_command` 退出码 0 → `code=COMMAND_COMPLETED`；非 0 → `code=COMMAND_EXIT_NONZERO`（`ok=true`，工具状态仍为 `succeeded`/「完成」而非工具失败），`hint` 含「退出码非零」/「不要当作成功」，`data.exit_code` 为实际退出码。
- `command-errors`：`execute_command` 的其他结果码（`command_execution.py`）：
  - `ok:false`：`EMPTY_COMMAND`（「command 不能为空」）、`CWD_NOT_FOUND`（「工作目录不存在: …」）、`CWD_NOT_A_DIRECTORY`（「工作目录不是目录: …」）、`SHELL_NOT_FOUND`（带 `shell`、`executable`）、`COMMAND_START_FAILED`、`COMMAND_TIMEOUT`（「命令执行超过 N 秒，已因超时终止，可能已部分执行。」，`data.output_is_result=false`）、`COMMAND_IO_FAILED`（「命令管道 I/O 失败，采集结果可能不完整」）。
  - `ok:null`（结果不确定，工具状态投影为 `unknown`/「中断」）：`COMMAND_INTERRUPTED`（「命令被用户停止，可能已部分执行。」）。
- `command-authorization`：`execute_command` 只有 `cwd`、`command`、`timeout_seconds` 三个参数，每次调用都需要授权（自动放行开启时被自动批准）；工具描述写明直接作用于真实目录、不提供回退，用 `git diff` / `git show` 核对改动。
- `search-tools`：`glob`、`grep` 在工作区内查找；`glob` 依赖 `fd`/`fdfind`，找不到时返回 `GLOB_NOT_FOUND`（「未找到 fd」）。
- `write-tools`：`write_file`（新建/覆盖）、`replace_all`（全量替换）。
- `argument-validation`：任一工具参数不符合 schema（如 `timeout_seconds` 超出 1–300）→ `ok:false`、`code:VALIDATION_ERROR`、`hint`「按工具 schema 修正参数后重试。」（`executor.py`）。
- `session-review-entry`：Agent 会话输入框上方的 Git 差异摘要与 `文件改动` 内容页，独立验证见 [session-file-changes.md](./session-file-changes.md)。
- `processes`：`start_process`（唯一需授权的进程工具）启动跨调用的持续程序，`read_process`/`list_processes`/`stop_process` 查看与停止（`process_management.py`）。成功码依次为 `PROCESS_STARTED`、`PROCESS_READ`、`PROCESS_STOPPED`、`PROCESSES_LISTED`；未知进程 `PROCESS_NOT_FOUND`。仅在 `win32`、`linux` 的顶层会话注册（子 Agent 与 macOS 没有这组工具，`assistant/assembly.py`）。判据：停止后端口不再监听，`list_processes` 显示已结束。
- `file-side-effect`：改动真实落盘到 Agent 工作树（见 Gotchas），人自己的 `$SCRATCH_WORKSPACE` 不变。
- `project-instructions`：每次决策重读工作区根目录的项目说明（`redpanda/assistant/context/prompt.py`）：优先 `AGENTS.md`；`AGENTS.md` 不存在或读不了时改读 `CLAUDE.md`；两者都读不了则无此块。读到的文件去掉首尾空白后为空 → 不加块，也**不**回退到 `CLAUDE.md`。内容作为 `<project_instructions>` 块拼在系统提示末尾，块内写明来源文件名；超 16000 字符截断并提示用 `read_file` 读完整文件。判据：在 `AGENTS.md` 写一条约定（如回答末尾加固定词），模型下一轮遵守。

`live_model_ready` 为 true 才跑本条。

## How to get to it (user POV)

- 打开会话（见 web-session-chat），打开开关 `自动放行写文件等工具`，在 `消息` 里点名要用的工具后 `发送`。

## Driving it with the browser

前置：`$SCRATCH_WORKSPACE` 是已提交 `note.txt`（内容 `verify`）的 Git 仓库，已在名册里用它创建 Agent；Doctor 返回 0。

- **开开关**：`browser click --role switch --name "自动放行写文件等工具"`；`GET /api/sessions/<id>` 中 `session.auto_authorize` 为 true。
- **发请求**：

```text
请只做这三件事，不要委派，不要安装依赖。
1. 用 read_file 读取 note.txt。
2. 用 apply_patch 把文件内容改成 edited。
3. 用 execute_command 执行 printf '%s\n' edited。
最后回复 CODING_ROUND_DONE。
```

- **判据**：
  - (a) `note.txt` 内容为 `edited`；`GET /api/sessions/<id>` 的 `items` 里有依次调用这三个工具的步骤，每个工具 `status` 为 `succeeded`。界面上成功步骤的标题行不显示状态徽标，展开后工具徽标为 `完成`（带勾）。
  - (b) 不出现待处理条（区域 `待处理`）；回复含 `CODING_ROUND_DONE`。
  - **命令退出码**（同会话或后续轮，自动放行开启）：
    1. `execute_command` 跑 `bash -c 'echo boom >&2; exit 2'`→ journal/`outcome.value` 中 `ok=true`、`code=COMMAND_EXIT_NONZERO`、`data.exit_code=2`、`hint` 含「退出码非零」或「不要当作成功」；该工具 `status` 为 `succeeded`（「完成」），不是工具失败。
    2. 再跑 `printf 'ok\n'`（或 `true`）→ `code=COMMAND_COMPLETED`、`data.exit_code=0`。
    Windows 上可用 `Write-Error "failed"` 作非零对照（本条 Linux 验证不覆盖）。
  - (c) 工作区里除 `note.txt` 外没有新增/修改文件（在 Agent 工作树里对比运行前后的 `git status --porcelain`）；`$SCRATCH_WORKSPACE` 本身不变；`$REDPANDA_HOME` 不出现在工作区内。
  - (d) 错误路径①：让模型 `read_file` 读 `../outside.txt`（工作区外）→ 非完全访问工作区应返回工具错误，文件内容不得出现在回复里。错误路径②：`apply_patch` 打一个上下文不匹配的补丁 → 工具报错，`note.txt` 不变。错误路径③：让模型 `execute_command` 用 `cwd` 指向不存在的子目录 → `CWD_NOT_FOUND`；`timeout_seconds=1` 跑 `sleep 5` → `COMMAND_TIMEOUT`，`data.output_is_result=false`。
- **错误路径（工具结果，不打崩会话）**：`glob` 的 pattern 为 `""` / `"."` / 仅空白时返回工具结果 `ok:false`、`code:EMPTY_PATTERN`，会话与 Worker 继续存活。参数不合 schema → `VALIDATION_ERROR`。handler 抛出的普通 `Exception`（如未预期 `ValueError`）由 `ToolsExecutor._execute_spec` 转为工具结果 `ok:false`、`code:TOOL_HANDLER_ERROR`（`data.exception_type` 为异常类名），**不**升级为调度器/Worker failure；`asyncio.CancelledError` / `KeyboardInterrupt` / `SystemExit` 等 `BaseException` 仍上抛。
- **搜索/写入**：让模型 `glob` 找 `*.txt`、`grep` 找 `edited`、`write_file` 新建 `new.txt` 写 `hi`、`replace_all` 把 `hi` 换成 `yo`。(a) `new.txt` 为 `yo`。(d) 实例 PATH 里没有 `fd`/`fdfind` 时 `glob` → `GLOB_NOT_FOUND`，记录为环境问题而非功能通过。
- **命令授权**：在**关闭**自动放行的会话里让模型 `execute_command` 跑一个只读命令（如 `git status`）→ (b) 待处理条出现该调用（自动放行开启时所有待授权调用都会被自动批准，看不到这一步）；(a) 批准后才执行。(c) 不存在按命令文本放行的情况。(d) 拒绝后命令不执行，模型收到「用户拒绝」。
- **持续进程**：让模型 `start_process` 起 `python -m http.server <端口>`，`read_process`、`list_processes`，再 `stop_process`。(a) 各步 `code` 依次为 `PROCESS_STARTED`、`PROCESS_READ`、`PROCESSES_LISTED`、`PROCESS_STOPPED`。(b) 自动放行关闭时，四个进程工具中只有 `start_process` 进入待处理条（`待授权`）。(c) 停止后端口不再监听。(d) 对不存在的 `process_id` 调 `read_process` → `PROCESS_NOT_FOUND`。
- **证据**：三个工具名可见时快照+截图；会话 JSON 与 `note.txt` 副本存到 `$EVIDENCE_DIR/tool-coding-round/`。
- **关开关的反向路径**：新开会话，开关保持关闭，发 `请用 apply_patch 把 note.txt 改成 edited，然后停止。`，在待处理条点 `允许 apply_patch`（详见 tool-authorization）；`read_file` 是只读工具，不会进入待处理。

## Gotchas

- Web 会话即名册里的 Agent，任务根是它自己的 worktree（`$REDPANDA_HOME/state/coding_bot/<项目>/<Agent>/`），不是 `$SCRATCH_WORKSPACE` 本身；判据里的「磁盘文件」读 Agent 工作树。Agent 通过命令主动暂存/提交、落地与同步（`land_branch`、`sync_branch`），Step 收口不自动暂存或提交。

- 非零退出不是工具失败：`ok` 仍为 true，`code` 为 `COMMAND_EXIT_NONZERO`（不是 `COMMAND_COMPLETED`）；模型应读 hint/stderr，不要把非零当作成功。
- `ok:null` 的结果（`COMMAND_INTERRUPTED`、`COMMAND_CONSTRAINT_FAILED`）在时间线是「中断」，不是「失败」；不要按失败判。
- 所有写入都不可回退；撤销靠 Git。
- 开关按会话保存，新会话默认关闭。
- 未配置供应商时 `发送` 是禁用的，不要直接 POST 输入。
- 持续进程工具在 macOS 不注册。
- `pytest -m process` 走的是同一批工具但不经 Web，只能记为进程测试。
- 步骤上没有 `从这一步之后重开`。
- 模型可能拒绝或换工具；通过条件是三个工具名按顺序出现且 `note.txt` 为 `edited`。
- 项目说明只认工作区根目录；`AGENTS.md` 存在但为空时不会改读 `CLAUDE.md`，验证回退时要先删除（或使其不可读）`AGENTS.md`。

### Live status

- 2026-10-18 改写：文件回退移除后，删除 `file_access`/`workspace_effect`、`get_changes`、`COMMAND_CONSTRAINT_FAILED` 与原生依赖相关判据；命令一律授权。未 live 复跑。
- 2026-10-18 源码核对（未 live）：补充 `execute_command` 其余结果码、`file_access`、`GLOB_NOT_FOUND`、`CHANGES_READ_FAILED`、`VALIDATION_ERROR`、进程工具结果码与平台范围、项目说明 `CLAUDE.md` 回退；均未驱动验证。
- 2026-10-16（pr25-verify merge @e701ef5，Web+DeepSeek）：coding-tools 通过；`bash -c 'exit 2'` → `COMMAND_EXIT_NONZERO` ok=true exit_code=2；`printf ok` → `COMMAND_COMPLETED` exit_code=0
- 2026-10-16（main@e191eab，仅 API，界面未验证）：`read_file`→`apply_patch`→`execute_command` 落盘；`start_process`/`read_process`/`list_processes`/`stop_process` 全部成功，停止后端口释放
- 通过（2026-10-12，main@1a4790a）
- PR #22（2026-10-16）：`EMPTY_PATTERN` 与 `TOOL_HANDLER_ERROR` 路径经 ToolsExecutor 直测 + Web live（glob `.`）通过；会话未崩。
- 已知产品缺口（2026-10-18 Windows live）：Windows 上 `execute_command` 的非零退出码一律记为 `exit_code=1`（`cmd /c exit 2` 等三种写法均为 1，journal 原始值确认）。原因是 PowerShell `-Command` 包装不传递原生 `$LASTEXITCODE`；`code=COMMAND_EXIT_NONZERO` 本身正确。修复前在 Windows 上不要用具体退出码作判据。
