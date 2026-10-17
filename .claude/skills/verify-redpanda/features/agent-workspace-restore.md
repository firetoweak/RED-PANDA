# 模型撤回文件变化（restore_workspace）

用户在对话里让助手「撤销刚才的修改」，助手调用 `restore_workspace`，按 `tool_call_id` 回退该批及其后助手造成的文件变化；回退本身也可再撤销。来源：`redpanda/tools/builtin/workspace_restore.py`（工具描述即规格）、`redpanda/assistant/workspace_versions.py`（`WorkspaceVersionBoundary.restore`、`restored_file_result`、`workspace_file_result_note`）、`redpanda/filesystem/operations.py`（冲突原因表）。

## Sub-features

- `restore-preserve`：`policy=preserve`（默认），位置后来被改过就保留后来的值。
- `restore-original`：`policy=original`，这些位置也恢复为原值；`hint` 额外带「policy=original 可能覆盖或删除用户后来的修改，请核对。」。
- `undo-restore`：引用这次 `restore_workspace` 的调用 id 再调用一次即撤销回退。
- `report`：成功结果 `ok:true`、`code:WORKSPACE_RESTORED`、`tool_call_id`、`data`、`hint`。`data` 为 `{policy, effect, changed_files, preserved_files, deleted_files, failed_files, error}`；`effect` 取 `restored` / `partially_restored` / `preserved` / `unchanged`（`unchanged`、`preserved` 是 `effect` 的取值，不是路径列表）。`hint` 按 effect 给出说明（如 `restored`：「已执行文件回退，请核对实际修改和删除的文件。」；`unchanged`：「本次文件处理没有改变文件。」），并都以「按用户目标核对当前文件，必要时用文件工具修正；不要反复重试。」结尾。
- `errors`：
  - `UNKNOWN_TOOL_CALL`：「当前上下文没有该调用。」
  - `WORKSPACE_VERSION_UNAVAILABLE`：「无法回退到该调用之前：那时的文件状态没有保存下来。」
  - `WORKSPACE_RESTORE_FAILED`：冲突时 `error` 为 `无法回退 <path>：<原因>。`，带 `failed_files`（只含第一个冲突路径），`hint` 以「本次回退没有修改文件。」开头；文件视图不可用时 `error` 为「当前无法回退工作区文件，请让用户检查应用安装。」，无 `failed_files`。
- `tool-description`：顶层会话描述要点：范围含同一工作区其他会话在此期间的助手修改；「unchanged 或 preserved 表示没有完全撤回」；「遇到混合修改、保留或失败时，读取当前内容后按用户目标用文件工具处理，不要换 policy 反复重试」；「必须单独调用」。子 Agent 使用 `ISOLATED_RESTORE_DESCRIPTION`（只恢复自己的工作区；「没有实际改动时不能宣称已撤回」）。

## How to get to it (user POV)

- 在 tool-coding-round 之后发 `请撤销你刚才对 note.txt 的修改。`

## Driving it with the browser

- **撤回**：(a) `note.txt` 回到 `verify`；会话历史不被截短（与 file-rewind 区别）；结果 `data.effect=restored`、`data.changed_files` 含 `note.txt`。(b) 步骤里出现 `restore_workspace`，`code=WORKSPACE_RESTORED`。(c) 助手未改过的文件、`.gitignore` 忽略但助手未碰的文件保持不变。(d) 见下方错误路径。
- **preserve 保留用户改动**：助手把 `a.txt` 写成 `A1`，用户手动原地改成同长的 `U1`，再让助手撤回（preserve）。(a) `a.txt` 仍为 `U1`；`data.effect` 为 `preserved` 或 `partially_restored`，`data.preserved_files` 含 `a.txt`。(b) `hint` 说明保留了后续值。(c) 其他文件按各自结果处理。(d) 此时回复若宣称已经完全撤回 → 判模型/提示问题（描述写明 preserved 表示没有完全撤回）；模型换成 `policy=original` 反复重试同样判为违反描述。
- **policy=original**：同样场景让助手显式用 `policy=original`。(a) 同长时 `a.txt` 回到助手修改前的值，人的修改被覆盖。(b) `hint` 含「policy=original 可能覆盖或删除用户后来的修改，请核对。」。(c) 助手没改过的位置保持不变。
- **撤销回退**：再让助手撤销刚才的撤回。(a) `note.txt` 回到 `edited`。
- **错误路径**：(d) 让它引用一个不存在的 `tool_call_id` → `UNKNOWN_TOOL_CALL`，文件不变。人工把助手改过的文件改成不同长度后再撤回 → `WORKSPACE_RESTORE_FAILED`，`error` 为 `无法回退 <path>：文件长度已变化，无法安全恢复。`，`failed_files=[<path>]`，所有文件不变。

## Gotchas

- 范围包含同一工作区其间其他会话的助手修改；并行会话验证时要预期这一点。
- 需要 `sandbox.ready` 与 `live_model_ready`。
- 冲突时只报第一个冲突路径，整次回退不改任何文件。
- 旧版描述里的「已完全撤销」措辞已不存在；判据以当前描述「unchanged 或 preserved 表示没有完全撤回」为准。

### 成功条件补充

- preserve 刻意保留人的改动；结果把文件标为 preserved/partially_restored，且回复不宣称已完全撤回即通过。字节混合可接受。

### Live status

- 2026-10-18 源码核对（未 live）：结果改为 `data{policy, effect, changed_files, preserved_files, deleted_files, failed_files, error}` + `hint`；补齐 `UNKNOWN_TOOL_CALL`、`WORKSPACE_VERSION_UNAVAILABLE`、`WORKSPACE_RESTORE_FAILED`（含 `failed_files`）；工具描述与子 Agent 描述按当前源码更新。
- 需复跑（r2，早于 6c9f456 `publish 遇宿主偏离时按 preserve 合并`）：r2 记录「长度变化时 `WORKSPACE_RESTORE_FAILED` 且不改文件；preserve 结果带 preserved 标记」。源码中长度检查仍在（`native/filesystem/src/restore.rs` `choose`），但宿主偏离的发布路径已改，需在当前 main 复跑后再当作通过。
- 观察（r2）：同长度 `policy=original` 会把人的 `HUMAN` 盖回 `hello`，hint 写明原值策略允许覆盖——属于模型主动选 original 的语义，不按 bug 报。
