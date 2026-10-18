# 回滚边界（用户时间旅行已删除）

不要再验证编辑消息、从一步重开、从一轮切新会话，也不要调用 `POST .../restarts`、`POST .../forks`、`POST .../branches`。这些入口已经去掉，见 [edit-and-branch](./edit-and-branch.md) 与 [file-rewind](./file-rewind.md)。

模型自己撤回文件仍走 [agent-workspace-restore](./agent-workspace-restore.md)。

## Sub-features

- 用户时间线上没有重开、改写、另开会话。
- 模型 `restore_workspace` 仍可用。

## How to get to it (user POV)

- 打开一轮已经跑过工具的会话，确认步骤上没有 `从这一步之后重开`，用户消息上没有 `编辑消息`。

## Driving it with the browser

- **入口缺失**：(a) 会话列表不因这些操作出现新身份，也不写 `lineage.json`。(b) 上述 POST 不是 201。(c) 原会话历史不变。(d) 不要把 `restore_workspace` 的结果当成用户重开。

## Gotchas

- 压缩、附件继承、人机混合修改若只在旧的用户重开路径上出现，这条不再覆盖。混合修改由模型回退的 preserve / original 策略处理。
