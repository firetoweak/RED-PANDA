# 改写消息与分支（已删除）

Web 不再提供编辑已发出的消息并重跑，也不再提供从一轮之后另开会话。复制消息仍在。

## Sub-features

- 没有 `编辑消息`、`改写并执行`、`从这一轮之后建立新会话`。
- 没有 `POST /api/sessions/{id}/forks` 与 `POST /api/sessions/{id}/branches`。

## How to get to it (user POV)

- 打开任意会话，悬停用户消息与轮次末尾。

## Driving it with the browser

- **入口缺失**：(a) 侧栏仍列出原会话，没有新的顶替身份。(b) 页面上没有上述按钮；直接 `POST .../forks` 或 `POST .../branches` 不是 201。(c) `复制消息` 仍把原文写入剪贴板。(d) 不要把 404 当成功能还在。

## Gotchas

- 模型调用的 `restore_workspace` 不在这条里，见 [agent-workspace-restore](./agent-workspace-restore.md)。
