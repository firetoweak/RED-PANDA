# 工具编码回合

在会话里打开 `自动放行写文件等工具`，让模型改文件、跑命令，时间线依次出现 `read_file`、`apply_patch`、`execute_command`，磁盘上的文件真的变了。工具来源 `redpanda/tools/builtin/`（`file_read.py`、`file_write.py`、`file_manage.py`、`command_execution.py`、`get_changes.py`），文件视图由 `native/sandbox` 提供。

## Sub-features

- `coding-tools`：一条消息内跑 `read_file` → `apply_patch` → `execute_command`。
- `search-tools`：`glob`、`grep` 在工作区内查找。
- `write-tools`：`write_file`（新建/覆盖）、`replace_all`（全量替换）。
- `get-changes`：`get_changes` 报告本会话改过的文件。
- `file-side-effect`：改动真实落盘到 `$SCRATCH_WORKSPACE`。

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
  - (a) `note.txt` 内容为 `edited`；`GET /api/sessions/<id>` 的 `items` 里有依次调用这三个工具的步骤，每步状态 `完成`。
  - (b) 不出现 `等待授权`；回复含 `CODING_ROUND_DONE`。
  - (c) 工作区里除 `note.txt` 外没有新增/修改文件（对比运行前 `find $SCRATCH_WORKSPACE -type f -newer` 的结果）；`$REDPANDA_HOME` 不出现在工作区内。
  - (d) 错误路径①：让模型 `read_file` 读 `../outside.txt`（工作区外）→ 非完全访问工作区应返回工具错误，文件内容不得出现在回复里。错误路径②：`apply_patch` 打一个上下文不匹配的补丁 → 工具报错，`note.txt` 不变。错误路径③：停止 sandbox 依赖（例如 `redpanda-sandbox` 二进制缺失）→ 时间线显示工具失败，不得「假装改好」。
- **搜索/写入**：让模型 `glob` 找 `*.txt`、`grep` 找 `edited`、`write_file` 新建 `new.txt` 写 `hi`、`replace_all` 把 `hi` 换成 `yo`、最后 `get_changes`。(a) `new.txt` 为 `yo`；`get_changes` 结果列出 `note.txt` 与 `new.txt`，不列未改动文件。
- **证据**：三个工具名可见时快照+截图；会话 JSON 与 `note.txt` 副本存到 `$EVIDENCE_DIR/tool-coding-round/`。
- **关开关的反向路径**：新开会话，开关保持关闭，发 `请 read_file 读取 note.txt，然后停止。`，对话框 `等待授权` 点 `允许`（详见 tool-authorization）。

## Gotchas

- 开关按会话保存，新会话默认关闭。
- 未配置供应商时 `发送` 是禁用的，不要直接 POST 输入。
- macOS 没有文件视图；缺 `redpanda-sandbox` 或 `fusermount3` 时工具会在时间线里失败，Doctor 应已跳过。
- `pytest -m process` 走的是同一批工具但不经 Web，只能记为进程测试。
- 只读命令不发布文件变化；`read_file` 那一步仍可能显示 `从这一步之后重开`，其保存的文件是 `apply_patch` 之前的字节，file-rewind 用的就是这一行。
- 模型可能拒绝或换工具；通过条件是三个工具名按顺序出现且 `note.txt` 为 `edited`。
