# 模型撤回文件变化（restore_workspace）

用户在对话里让助手「撤销刚才的修改」，助手调用 `restore_workspace`，按 `tool_call_id` 回退该批及其后助手造成的文件变化；回退本身也可再撤销。来源：`redpanda/tools/builtin/workspace_restore.py`（工具描述即规格）、`redpanda/assistant/workspace_versions.py`。

## Sub-features

- `restore-preserve`：`policy=preserve`，保留用户后来改过的值。
- `restore-original`：`policy=original`，允许恢复助手改动位置的原值。
- `undo-restore`：引用这次 `restore_workspace` 的调用 id 再调用一次即撤销回退。
- `report`：结果列出实际改动、`unchanged`、`preserved` 的路径。

## How to get to it (user POV)

- 在 tool-coding-round 之后发 `请撤销你刚才对 note.txt 的修改。`

## Driving it with the browser

- **撤回**：(a) `note.txt` 回到 `verify`；会话历史不被截短（与 file-rewind 区别）。(b) 步骤里出现 `restore_workspace`，结果列出 `note.txt`。(c) 助手未改过的文件、`.gitignore` 忽略但助手未碰的文件保持不变。
- **preserve 保留用户改动**：助手把 `a.txt` 写成 `A1`，用户手动改成 `U1`，再让助手撤回（preserve）。(a) `a.txt` 仍为 `U1`，结果把它标为 `preserved`。(d) 此时回复若宣称「已完全撤销」→ 判模型/提示问题（描述明确禁止）。
- **撤销回退**：再让助手撤销刚才的撤回。(a) `note.txt` 回到 `edited`。
- **错误路径**：(d) 让它引用一个不存在的 `tool_call_id` → 工具报错，文件不变。

## Gotchas

- 范围包含同一工作区其间其他会话的助手修改；并行会话验证时要预期这一点。
- 需要 `sandbox.ready` 与 `live_model_ready`。

## 成功条件补充

- preserve 刻意保留人的改动；结果把文件标为 preserved/partially_restored，且回复不宣称「已完全撤销」即通过。字节混合可接受。

## Live status
- 通过（r2，按新条件）：长度变化时 `WORKSPACE_RESTORE_FAILED` 且不改文件；preserve 结果带 preserved 标记
- 观察：同长度 `policy=original` 会把人的 `HUMAN` 盖回 `hello`，hint 写明原值策略允许覆盖——属于模型主动选 original 的语义，不按 bug 报
