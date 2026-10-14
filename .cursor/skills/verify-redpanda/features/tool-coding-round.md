# 工具编码回合

在会话里打开 `自动放行写文件等工具`，让模型改文件、跑命令，时间线依次出现 `read_file`、`apply_patch`、`execute_command`，磁盘上的文件真的变了。工具来源 `redpanda/tools/builtin/`（`file_read.py`、`file_write.py`、`file_manage.py`、`command_execution.py`、`get_changes.py`），文件视图由 `native/filesystem` 提供。

## Sub-features

- `coding-tools`：一条消息内跑 `read_file` → `apply_patch` → `execute_command`。
- `search-tools`：`glob`、`grep` 在工作区内查找。
- `write-tools`：`write_file`（新建/覆盖）、`replace_all`（全量替换）。
- `get-changes`：`get_changes` 默认 `scope=session`，核对当前会话（含分支继承）涉及的文件及当前变化，区分当前会话、会话外、混合与未知来源；显式 `scope=workspace` 才核对工作区所有会话的助手修改。
- `session-review-entry`：输入框上方的会话净变更摘要与 `会话变更` 抽屉，独立验证见 [session-file-changes.md](./session-file-changes.md)。
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
- **搜索/写入**：让模型 `glob` 找 `*.txt`、`grep` 找 `edited`、`write_file` 新建 `new.txt` 写 `hi`、`replace_all` 把 `hi` 换成 `yo`、最后 `get_changes`。(a) `new.txt` 为 `yo`；`get_changes` 结果包含 `note.txt` 与 `new.txt`，不列助手从未涉及的文件；已恢复原值的已触碰文件可以以 `unchanged` 出现。按 `next_offset`、`next_text_offset` 续读，不能把截断页当作完整成果。
- **查询范围**：同一工作区会话 A 新建 `a.txt`，独立新建会话 B 并让模型先调用不带 scope 的 `get_changes`，应返回 `scope=session` 且不包含 `a.txt`；再明确请求 `scope=workspace`，应返回 `scope=workspace` 并能核对 A 的修改。B 新建 `b.txt` 后，其默认查询只检查 B 涉及的文件。若 B 修改 A 已修改的同一文件，A 的默认查询需把 B 后来的内容标为 `external` 或 `mixed`，不能据此宣称都是 A 的成果；scope 取非法值应参数校验失败。分支通过真实入口继承 A 后，默认范围包含继承的修改。续读必须保持 scope 不变。
- **证据**：三个工具名可见时快照+截图；会话 JSON 与 `note.txt` 副本存到 `$EVIDENCE_DIR/tool-coding-round/`。
- **关开关的反向路径**：新开会话，开关保持关闭，发 `请 read_file 读取 note.txt，然后停止。`，对话框 `等待授权` 点 `允许`（详见 tool-authorization）。

## Gotchas

- 开关按会话保存，新会话默认关闭。
- 未配置供应商时 `发送` 是禁用的，不要直接 POST 输入。
- macOS 没有文件视图；缺 `redpanda-sandbox` 或 `fusermount3` 时工具会在时间线里失败，Doctor 应已跳过。
- `pytest -m process` 走的是同一批工具但不经 Web，只能记为进程测试。
- 只读命令不发布文件变化；`read_file` 那一步仍可能显示 `从这一步之后重开`，其保存的文件是 `apply_patch` 之前的字节，file-rewind 用的就是这一行。
- 模型可能拒绝或换工具；通过条件是三个工具名按顺序出现且 `note.txt` 为 `edited`。
- 当前 `get_changes` 默认与面板同为会话范围；只有显式 `scope=workspace` 才扩大范围。工具的 `external`、`unchanged` 等状态用于解释成果是否仍保留，不意味着这些内容属于本会话成果。2026-10-13 新增默认会话范围与显式工作区范围的源码/自动化验证，未更新下方历史 live 状态。
- 删除后的文件同样要检查修改前证据是否完整；当前文件不存在不能作为 `content_complete=true` 的依据，原文缺失时必须显示限制说明。

## Live status
- 通过（2026-10-12，main@1a4790a）
