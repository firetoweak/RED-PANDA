# 文件回滚（从这一步之后重开，已删除）

Web 不再提供从某一步重开会话，也不再提供连同文件一起回滚的对话框。步骤上不出现 `从这一步之后重开`。

## Sub-features

- 没有 `从这一步之后重开`、`只回滚会话`、`会话和文件一起回滚`。
- 没有 `POST /api/sessions/{id}/restarts`。
- 不写 `runtime_sessions/lineage.json`。

## How to get to it (user POV)

- 完成一轮带工具的对话，展开执行过程。

## Driving it with the browser

- **入口缺失**：(a) 成功步骤没有重开按钮；工作区文件保持现状。(b) 直接 `POST .../restarts` 不是 201。(c) 原会话仍在 `GET /api/sessions` 列表中。(d) 不要把模型的 `restore_workspace` 当成这条用户入口。

## Gotchas

- 模型自己撤回文件仍走 [agent-workspace-restore](./agent-workspace-restore.md)。
