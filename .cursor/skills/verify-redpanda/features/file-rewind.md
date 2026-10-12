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
- **只回滚会话**：(a) 会话截短，`note.txt` 保持 `edited`。(c) 工作区所有文件不变。
- **证据**：截短后的转录截图（带侧栏）、会话 JSON、恢复后的 `note.txt` 存到 `$EVIDENCE_DIR/file-rewind/`。

## Gotchas

- 只有有成功文件版本的步骤才渲染回滚按钮，按钮缺失不是被隐藏。
- `只回滚会话` 不能证明本功能。
- 被判为只读的命令即便写了文件也不在回滚范围内。
- 同一工作区其间其他会话的助手修改也可能被一起回退（见 agent-workspace-restore 中工具描述），多会话并行时要记录。

## Live status
- 部分（2026-10-12，修复后）：机械回退结果分类（restored / partially_restored / preserved / kept / failed）对用户与模型两侧可见（源码确认 + 自动化通过）；人机混改不再静默宣称完全恢复
- 未验（待真实 UI）：警示与文件清单的实际展示；preserve / original 的具体语义效果由模型或用户按目标处理
- A_overlap、A_same、B2`