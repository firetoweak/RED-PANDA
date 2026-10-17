# 工具编码回合

在会话里打开 `自动放行写文件等工具`，让模型改文件、跑命令，时间线依次出现 `read_file`、`apply_patch`、`execute_command`，磁盘上的文件真的变了。工具来源 `redpanda/tools/builtin/`（`file_read.py`、`file_write.py`、`file_manage.py`、`command_execution.py`、`get_changes.py`、`process_management.py`），参数校验与 handler 异常转换在 `redpanda/tools/executor.py`。受管文件视图的 Python 侧在 `redpanda/filesystem/file_view/`（`client.py`、`native*.py`、`publication.py`），由它驱动 `native/filesystem` 构建出的 Rust 程序（`redpanda-agentfs`）。

## Sub-features

- `coding-tools`：一条消息内跑 `read_file` → `apply_patch` → `execute_command`。
- `command-exit-codes`：`execute_command` 退出码 0 → `code=COMMAND_COMPLETED`；非 0 → `code=COMMAND_EXIT_NONZERO`（`ok=true`，工具状态仍为 `succeeded`/「完成」而非工具失败），`hint` 含「退出码非零」/「不要当作成功」，`data.exit_code` 为实际退出码。
- `command-errors`：`execute_command` 的其他结果码（`command_execution.py`）：
  - `ok:false`：`EMPTY_COMMAND`（「command 不能为空」）、`CWD_NOT_FOUND`（「工作目录不存在: …」）、`CWD_NOT_A_DIRECTORY`（「工作目录不是目录: …」）、`SHELL_NOT_FOUND`（带 `shell`、`executable`）、`COMMAND_START_FAILED`、`COMMAND_TIMEOUT`（「命令执行超过 N 秒，已因超时终止，可能已部分执行。」，`data.output_is_result=false`）、`COMMAND_IO_FAILED`（「命令管道 I/O 失败，采集结果可能不完整」）。
  - `ok:null`（结果不确定，工具状态投影为 `unknown`/「中断」）：`COMMAND_CONSTRAINT_FAILED`（不受支持的进程启动方式或无法满足本机执行限制，`data.exit_code=null`）、`COMMAND_INTERRUPTED`（「命令被用户停止，可能已部分执行。」）。
- `command-file-access`：`execute_command` 参数 `file_access`：`managed`（默认，工作区内修改可用 `get_changes` 核对、可回退）或 `host`（直接访问本机文件，不可回退）。`host` 且 `workspace_effect=may_write` 时需要授权（`exec_policy.py` 的 `command_requires_authorization`）；工具描述要求 managed 失败时不要改用 host 重试。
- `search-tools`：`glob`、`grep` 在工作区内查找；`glob` 依赖 `fd`/`fdfind`，找不到时返回 `GLOB_NOT_FOUND`（「未找到 fd」）。
- `write-tools`：`write_file`（新建/覆盖）、`replace_all`（全量替换）。
- `get-changes`：`get_changes` 默认 `scope=session`，核对当前会话（含分支继承）涉及的文件及当前变化，区分当前会话、会话外、混合与未知来源；显式 `scope=workspace` 才核对工作区所有会话的助手修改。读取失败返回 `CHANGES_READ_FAILED`。
- `argument-validation`：任一工具参数不符合 schema（如 `scope` 取非法值、`timeout_seconds` 超出 1–300）→ `ok:false`、`code:VALIDATION_ERROR`、`hint`「按工具 schema 修正参数后重试。」（`executor.py`）。
- `session-review-entry`：输入框上方的会话净变更摘要与 `文件改动` 内容页，独立验证见 [session-file-changes.md](./session-file-changes.md)。
- `processes`：`start_process`（唯一需授权的进程工具）启动跨调用的持续程序，`read_process`/`list_processes`/`stop_process` 查看与停止（`process_management.py`）。成功码依次为 `PROCESS_STARTED`、`PROCESS_READ`、`PROCESS_STOPPED`、`PROCESSES_LISTED`；未知进程 `PROCESS_NOT_FOUND`。仅在 `win32`、`linux` 的顶层会话注册（子 Agent 与 macOS 没有这组工具，`assistant/assembly.py`）。判据：停止后端口不再监听，`list_processes` 显示已结束。
- `file-side-effect`：改动真实落盘到 `$SCRATCH_WORKSPACE`。
- `project-instructions`：每次决策重读工作区根目录的项目说明（`redpanda/assistant/context/prompt.py`）：优先 `AGENTS.md`；`AGENTS.md` 不存在或读不了时改读 `CLAUDE.md`；两者都读不了则无此块。读到的文件去掉首尾空白后为空 → 不加块，也**不**回退到 `CLAUDE.md`。内容作为 `<project_instructions>` 块拼在系统提示末尾，块内写明来源文件名；超 16000 字符截断并提示用 `read_file` 读完整文件。判据：在 `AGENTS.md` 写一条约定（如回答末尾加固定词），模型下一轮遵守。

`live_model_ready` 与 `sandbox.ready` 都为 true 才跑本条。

## How to get to it (user POV)

- 打开会话（见 web-session-chat），打开开关 `自动放行写文件等工具`，在 `消息` 里点名要用的工具后 `发送`。

## Driving it with the browser

前置：`note.txt` 为 `verify`，Doctor 返回 0。

- **开开关**：`browser click --role switch --name "自动放行写文件等工具"`；`GET /api/sessions/<id>` 中 `session.auto_authorize` 为 true。
- **发请求**：

```text
请只做这三件事，不要委派，不要安装依赖。
1. 用 read_file 读取 note.txt。
2. 用 apply_patch 把文件内容改成 edited。
3. 用 execute_command 执行 printf '%s\n' edited，workspace_effect 用 may_write。
最后回复 CODING_ROUND_DONE。
```

- **判据**：
  - (a) `note.txt` 内容为 `edited`；`GET /api/sessions/<id>` 的 `items` 里有依次调用这三个工具的步骤，每个工具 `status` 为 `succeeded`。界面上成功步骤的标题行不显示状态徽标，展开后工具徽标为 `完成`（带勾）。
  - (b) 不出现 `等待授权`；回复含 `CODING_ROUND_DONE`。
  - **命令退出码**（同会话或后续轮，自动放行开启）：
    1. `execute_command` 跑 `bash -c 'echo boom >&2; exit 2'`（`workspace_effect=read_only`）→ journal/`outcome.value` 中 `ok=true`、`code=COMMAND_EXIT_NONZERO`、`data.exit_code=2`、`hint` 含「退出码非零」或「不要当作成功」；该工具 `status` 为 `succeeded`（「完成」），不是工具失败。
    2. 再跑 `printf 'ok\n'`（或 `true`）→ `code=COMMAND_COMPLETED`、`data.exit_code=0`。
    Windows 上可用 `Write-Error "failed"` 作非零对照（本条 Linux 验证不覆盖）。
  - (c) 工作区里除 `note.txt` 外没有新增/修改文件（对比运行前 `find $SCRATCH_WORKSPACE -type f -newer` 的结果）；`$REDPANDA_HOME` 不出现在工作区内。
  - (d) 错误路径①：让模型 `read_file` 读 `../outside.txt`（工作区外）→ 非完全访问工作区应返回工具错误，文件内容不得出现在回复里。错误路径②：`apply_patch` 打一个上下文不匹配的补丁 → 工具报错，`note.txt` 不变。错误路径③：停止 sandbox 依赖（例如 `redpanda-agentfs` 二进制缺失）→ 时间线显示工具失败，不得「假装改好」。错误路径④：让模型 `execute_command` 用 `cwd` 指向不存在的子目录 → `CWD_NOT_FOUND`；`timeout_seconds=1` 跑 `sleep 5` → `COMMAND_TIMEOUT`，`data.output_is_result=false`。
- **错误路径（工具结果，不打崩会话）**：`glob` 的 pattern 为 `""` / `"."` / 仅空白时返回工具结果 `ok:false`、`code:EMPTY_PATTERN`，会话与 Worker 继续存活。参数不合 schema → `VALIDATION_ERROR`。handler 抛出的普通 `Exception`（如未预期 `ValueError`）由 `ToolsExecutor._execute_spec` 转为工具结果 `ok:false`、`code:TOOL_HANDLER_ERROR`（`data.exception_type` 为异常类名），**不**升级为调度器/Worker failure；`asyncio.CancelledError` / `KeyboardInterrupt` / `SystemExit` 等 `BaseException` 仍上抛。
- **搜索/写入**：让模型 `glob` 找 `*.txt`、`grep` 找 `edited`、`write_file` 新建 `new.txt` 写 `hi`、`replace_all` 把 `hi` 换成 `yo`、最后 `get_changes`。(a) `new.txt` 为 `yo`；`get_changes` 结果包含 `note.txt` 与 `new.txt`，不列助手从未涉及的文件；已恢复原值的已触碰文件可以以 `unchanged` 出现。按 `next_offset`、`next_text_offset` 续读，不能把截断页当作完整成果。(d) 实例 PATH 里没有 `fd`/`fdfind` 时 `glob` → `GLOB_NOT_FOUND`，记录为环境问题而非功能通过。
- **查询范围**：同一工作区会话 A 新建 `a.txt`，独立新建会话 B 并让模型先调用不带 scope 的 `get_changes`，应返回 `scope=session` 且不包含 `a.txt`；再明确请求 `scope=workspace`，应返回 `scope=workspace` 并能核对 A 的修改。B 新建 `b.txt` 后，其默认查询只检查 B 涉及的文件。若 B 修改 A 已修改的同一文件，A 的默认查询需把 B 后来的内容标为 `external` 或 `mixed`，不能据此宣称都是 A 的成果；scope 取非法值 → `VALIDATION_ERROR`。分支通过真实入口继承 A 后，默认范围包含继承的修改。续读必须保持 scope 不变。
- **宿主文件访问**：在**关闭**自动放行的会话里让模型 `execute_command` 以 `file_access=host`、`workspace_effect=may_write` 写一个文件 → (b) 出现 `等待授权`（自动放行开启时所有待授权调用都会被自动批准，看不到这一步）；(a) 批准后文件写到磁盘，但这次写入不在可回退范围内，file-rewind 不会撤销它。(c) `file_access=host` + `read_only` 的普通命令不要求授权。
- **持续进程**：让模型 `start_process` 起 `python -m http.server <端口>`，`read_process`、`list_processes`，再 `stop_process`。(a) 各步 `code` 依次为 `PROCESS_STARTED`、`PROCESS_READ`、`PROCESSES_LISTED`、`PROCESS_STOPPED`。(b) 自动放行关闭时，四个工具中只有 `start_process` 出现 `等待授权`。(c) 停止后端口不再监听。(d) 对不存在的 `process_id` 调 `read_process` → `PROCESS_NOT_FOUND`。
- **证据**：三个工具名可见时快照+截图；会话 JSON 与 `note.txt` 副本存到 `$EVIDENCE_DIR/tool-coding-round/`。
- **关开关的反向路径**：新开会话，开关保持关闭，发 `请 read_file 读取 note.txt，然后停止。`，对话框 `等待授权` 点 `允许`（详见 tool-authorization）。

## Gotchas

- 非零退出不是工具失败：`ok` 仍为 true，`code` 为 `COMMAND_EXIT_NONZERO`（不是 `COMMAND_COMPLETED`）；模型应读 hint/stderr，不要把非零当作成功。
- `ok:null` 的结果（`COMMAND_INTERRUPTED`、`COMMAND_CONSTRAINT_FAILED`）在时间线是「中断」，不是「失败」；不要按失败判。
- `file_access=host` 的写入不可回退；file-rewind 与 `restore_workspace` 都不处理它。
- 开关按会话保存，新会话默认关闭。
- 未配置供应商时 `发送` 是禁用的，不要直接 POST 输入。
- macOS 没有文件视图；缺 `redpanda-agentfs` 或 `fusermount3` 时工具会在时间线里失败，Doctor 应已跳过。持续进程工具在 macOS 不注册。
- `pytest -m process` 走的是同一批工具但不经 Web，只能记为进程测试。
- 只读命令不发布文件变化；`read_file` 那一步仍可能显示 `从这一步之后重开`，其保存的文件是 `apply_patch` 之前的字节，file-rewind 用的就是这一行。
- 模型可能拒绝或换工具；通过条件是三个工具名按顺序出现且 `note.txt` 为 `edited`。
- 当前 `get_changes` 默认与面板同为会话范围；只有显式 `scope=workspace` 才扩大范围。工具的 `external`、`unchanged` 等状态用于解释成果是否仍保留，不意味着这些内容属于本会话成果。2026-10-13 新增默认会话范围与显式工作区范围的源码/自动化验证，未更新下方历史 live 状态。
- 删除后的文件同样要检查修改前证据是否完整；当前文件不存在不能作为 `content_complete=true` 的依据，原文缺失时必须显示限制说明。
- 项目说明只认工作区根目录；`AGENTS.md` 存在但为空时不会改读 `CLAUDE.md`，验证回退时要先删除（或使其不可读）`AGENTS.md`。

### Live status

- 2026-10-18 源码核对（未 live）：补充 `execute_command` 其余结果码、`file_access`、`GLOB_NOT_FOUND`、`CHANGES_READ_FAILED`、`VALIDATION_ERROR`、进程工具结果码与平台范围、项目说明 `CLAUDE.md` 回退；均未驱动验证。
- 2026-10-16（pr25-verify merge @e701ef5，Web+DeepSeek）：coding-tools 通过；`bash -c 'exit 2'` → `COMMAND_EXIT_NONZERO` ok=true exit_code=2；`printf ok` → `COMMAND_COMPLETED` exit_code=0
- 2026-10-16（main@e191eab，仅 API，界面未验证）：`read_file`→`apply_patch`→`execute_command` 落盘；`start_process`/`read_process`/`list_processes`/`stop_process` 全部成功，停止后端口释放
- 通过（2026-10-12，main@1a4790a）
- PR #22（2026-10-16）：`EMPTY_PATTERN` 与 `TOOL_HANDLER_ERROR` 路径经 ToolsExecutor 直测 + Web live（glob `.`）通过；会话未崩。
- 已知产品缺口（2026-10-18 Windows live）：Windows 上 `execute_command` 的非零退出码一律记为 `exit_code=1`（`cmd /c exit 2` 等三种写法均为 1，journal 原始值确认）。原因是 PowerShell `-Command` 包装不传递原生 `$LASTEXITCODE`；`code=COMMAND_EXIT_NONZERO` 本身正确。修复前在 Windows 上不要用具体退出码作判据。
