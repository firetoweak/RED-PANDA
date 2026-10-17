# 文件回滚（从这一步之后重开）

在保存过工作区版本的步骤上选 `从这一步之后重开`，再选 `会话和文件一起回滚`：之后的对话消失，工作区文件回到那一步。来源：`Conversation.tsx`（对话框）、`ExecutionProcess.tsx`、`app.py` 的 `POST /api/sessions/{id}/restarts`（`restore_files`）、`redpanda/assistant/workspace_versions.py`、`redpanda/assistant/host/supervisor.py` 的 `restart_from_step`。

## Sub-features

- `rewind-open`：从可回滚的步骤打开对话框。
- `rewind-files`：`会话和文件一起回滚`（`restore_files: true`）。
- `rewind-session-only`：`只回滚会话`（`restore_files: false`），文件不动。
- `rewind-disk`：磁盘上的字节真的恢复。

需要已经跑过 tool-coding-round 产生可回滚步骤，且 `sandbox.ready`。macOS 始终跳过。

## How to get to it (user POV)

- 完成 tool-coding-round，使 `note.txt` 为 `edited`，至少一个步骤显示 `从这一步之后重开`。
- 在 `read_file` 那一步点 `从这一步之后重开`，对话框里点 `会话和文件一起回滚`。

## Driving it with the browser

前置：同一会话；若 `read_file` 与 `apply_patch` 在同一行，先发 `请只用 apply_patch 把 note.txt 改成 later，然后回复 LATER_DONE。`，等文件为 `later` 后回滚那一行。

- **一起回滚**：
  - (a) `note.txt` 逐字节等于该步完成时的内容（回滚 `read_file` 行 → `verify`；回滚合并行 → `edited`）。用户在其间自己新建、助手未碰过的文件（如事先手动建 `user.txt`）仍在且内容不变。`GET /api/sessions/<id>`：所选步骤保留，其后的步骤消失。
  - (b) `POST .../restarts` 返回 201；对话框关闭，之后的转录文字消失。
  - (c) 所选步骤之前的消息（原始编码请求）保留；工作区外写入、安装的依赖、网络调用不会被撤销（不作为失败）。
  - 错误路径②：文件未能完全恢复时 → 界面必须同时给出会话回退结果与文件处理结果：以醒目警示列出涉及文件与实际变更、保留、删除、未能处理的文件和原因，不能只显示「回滚完成」；模型侧收到同一结果与「读取当前内容、按用户目标修改」的引导。
- 注意：`POST .../restarts` 返回 201 的是**新会话**（`child_session_id`，body 里的 `session_id` 与原会话不同），截短后的转录在新会话里读；原会话保持原样。`workspace_file_result` 在响应体里（`policy`/`effect`/`changed_files`/`preserved_files`/`failed_files`/`error`）。
- **只回滚会话**：(a) 会话截短，`note.txt` 保持 `edited`。(c) 工作区所有文件不变。
- **证据**：截短后的转录截图（带侧栏）、会话 JSON、恢复后的 `note.txt` 存到 `$EVIDENCE_DIR/file-rewind/`。

## Gotchas

- 只有有成功文件版本的步骤才渲染回滚按钮，按钮缺失不是被隐藏。
- `只回滚会话` 不能证明本功能。
- 被判为只读的命令即便写了文件也不在回滚范围内。
- 同一工作区其间其他会话的助手修改也可能被一起回退（见 agent-workspace-restore 中工具描述），多会话并行时要记录。

## 成功条件补充

- 普通步骤回滚返回 201 是正常的，不是问题。
- 回滚不保证成功；失败时用户能在 `会话已回退 · 文件处理结果` 看到提示就算达标。
- 人手动（Web）回滚用 `preserve`：不覆盖人的改动，只回滚 agent 的部分。人机都改过的文件，界面须提示「此文件 agent 改过、人也改过，只回滚了 agent 的部分」；当前「文件部分回退，部分后续值已保留」未写清，记为剩余缺口。

## Live status
- 2026-10-16（main@e191eab，仅 API，界面未验证）：回滚 `read_file` 步 → 201 新会话 2 条、`effect: restored`、文件回到 `base`、用户文件不变；人机都改同一文件 → `effect: failed`+`error`（设计内可失败并提示）
- 通过（r2）：主路径 `AFTER_AGENT`→`before`，`restarts` 201（普通回滚 201 是正常结果）；失败与部分回退按新条件以界面提示为准
- **剩余缺口**：人机混合时的提示措辞（见上）
