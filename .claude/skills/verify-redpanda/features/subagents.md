# 子 Agent

主 Agent 用 `delegate` 派出子任务，子 Agent 在独立工作副本里干活，`report` 交回结论；主 Agent 可 `reclaim` 收回、`compare_subagent` 看差异、`merge_subagent` 合并。来源：`redpanda/assistant/subagent/subagent.py`、`redpanda/tools/builtin/subagent_workspace.py`、`redpanda/filesystem/children.py`、`SubagentCallCard.tsx`、`SubagentPanel.tsx`、`app.py` 的 `GET .../subagents/{command_id}`。

## Sub-features

- `delegate-card`：时间线出现子任务卡片，状态 `已派出`/`排队中`/`运行中`/`完成`/`失败`/`中断`，按钮 `查看过程`。
- `observe-panel`：`子 Agent 观察面板`，状态 `正在创建`/`运行中`/`已交回`/`已收回`/`已结束，未报告`/`执行失败`/`连接中断`，`交回结论`、`回到最新`、`关闭子 Agent 面板`、可拖宽（`调节子 Agent 面板宽度`）。
- `isolation`：子 Agent 改动写在 `$REDPANDA_HOME/state/subagent_worktrees/<key>`，不直接动用户文件。
- `compare-merge`：`compare_subagent` 先列文件再给 diff；`merge_subagent` 需要授权，合并进用户文件。
- `sidebar-wait`：主会话侧栏显示 `等待子 Agent`。

## How to get to it (user POV)

- 发 `请委派一个子 Agent 在 note.txt 末尾追加一行 child，完成后比较并合并它的修改。`

## Driving it with the browser

- **派出与观察**：(a) `GET .../subagents/<command_id>` 返回子会话过程；面板内容与之一致。(b) 卡片状态从 `已派出` 走到 `完成`，面板显示 `已交回`。(d) `GET .../subagents/不存在` → 404，面板显示 `无法读取子任务过程`。
- **隔离**：合并前 (a) `note.txt` 不含 `child`；`state/subagent_worktrees/` 下的副本含 `child`。(c) 合并前用户文件 mtime 不变。
- **合并**：`merge_subagent` 出现 `待授权` → `允许`。(a) `note.txt` 末尾多一行 `child`，原有内容保留。(d) 先在 `note.txt` 手动改同一行制造冲突 → 合并应报冲突或保留双方，不能静默覆盖用户改动。拒绝授权 → 文件不变。
- **收回**：子 Agent 运行中让主 Agent 收回（或中断主回合）→ 面板 `已收回`，`ps` 无残留子进程，副本不合入。

## Gotchas

- 没有 `sandbox.ready` 时副本无法创建，整条跳过。
- 模型可能不委派；通过条件是 `delegate` 工具真实出现在步骤里。

## Live status
- 通过（2026-10-12，main@1a4790a）
