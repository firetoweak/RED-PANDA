# 子 Agent

主 Agent 用 `delegate` 派出子任务，子 Agent 在独立工作副本里干活，`report` 交回结论；主 Agent 可 `reclaim` 收回、`compare_subagent` 看差异、`merge_subagent` 合并。来源：`redpanda/assistant/subagent/subagent.py`、`redpanda/assistant/subagent/review.py`、`redpanda/tools/builtin/subagent_workspace.py`、`redpanda/filesystem/children.py`、`redpanda/assistant/conversations.py`（`observe_subagent`）、`SubagentCallCard.tsx`、`SubagentPanel.tsx`、`ExecutionProcess.tsx`、`ContentPage.tsx`、`Conversation.tsx`、`app.py` 的 `GET .../subagents/{command_id}`。

## Sub-features

- `delegate-card`：时间线出现 `delegate` 卡片，徽标 `排队中`/`运行中`/`已派出`（delegate 调用成功）/`失败`/`中断`/`待授权`/`已拒绝`，下方有任务文字与 `查看过程`（`失败`、`已拒绝` 时禁用）。
- `turn-marker`：执行过程顶部 `本轮子 Agent 任务`，徽标 `子 Agent · N`，每个任务一个按钮（aria-label `查看子 Agent 过程：<task>`）。
- `observe-panel`：点 `查看过程` 或上面的任务按钮，右侧打开内容页，标题与 aria-label 都是 `子 Agent`；状态 `正在创建`/`运行中`/`等待中`/`已交回`/`已收回`/`已结束，未报告`/`执行失败`/`连接中断`；有 `交回结论` 区、`收回原因：<reason>`、`回到最新`；关闭按钮 `关闭内容页`，拖宽分隔条 `调节内容页宽度`。
- `parent-hints`：父会话最新一轮在子仍未回收时显示 `子 Agent 执行中`；子以失败结束时显示 Alert `子 Agent 执行失败`。
- `isolation`：子 Agent 改动写在 `$REDPANDA_HOME/state/subagent_worktrees/<key>`，不直接动用户文件。
- `compare-merge`：`compare_subagent` 先列文件再给 diff；`merge_subagent` 需要授权，合并进用户文件；冲突返回 `MERGE_CONFLICT`。
- `reclaim`：`reclaim` 结果码 `RECLAIMED`/`ALREADY_RECLAIMED`/`UNKNOWN_CHILD`/`RECLAIM_NOT_ALLOWED`。
- `sidebar-wait`：主会话侧栏显示 `等待子 Agent`。

## How to get to it (user POV)

- 发 `请委派一个子 Agent 在 note.txt 末尾追加一行 child，完成后比较并合并它的修改。`

## Driving it with the browser

- **派出与观察**：
  - (a) `GET /api/sessions/<父 id>/subagents/<command_id>` 返回 200 与子会话过程；内容页里的步骤与之一致。
  - (b) 卡片徽标走到 `已派出`；子运行期间父最新一轮显示 `子 Agent 执行中`；子交回后内容页状态 `已交回`，`交回结论` 区显示摘要。
  - (c) 关闭内容页（`关闭内容页`）不影响子的运行；重新用 `查看子 Agent 过程：<task>` 打开，内容一致。
  - (d) `GET .../subagents/不存在` → 404，body `{"detail":"委派不存在：不存在"}`；面板读取失败时显示 `无法读取子任务过程`。
- **隔离**：合并前 (a) `note.txt` 不含 `child`；`state/subagent_worktrees/` 下的副本含 `child`。(c) 合并前用户文件字节与 mtime 不变。(b) `compare_subagent` 结果只列 `note.txt`。(d) 没有 `sandbox.ready` 时副本无法创建，整条跳过并记录。
- **合并**：`merge_subagent` 卡片 `待授权` → 允许。
  - (a) `note.txt` 末尾多一行 `child`，原有内容保留；结果 `code=SUBAGENT_MERGED`，`changed_count` 与实际改动文件数一致。
  - (b) 授权前卡片 `待授权`，拒绝后 `已拒绝`。
  - (c) 拒绝授权 → 用户文件不变。
  - (d) 合并前先在 `note.txt` 末尾手动改同一处制造冲突 → 结果 `ok=false`、`code=MERGE_CONFLICT`、`data.conflicts` 列出 `note.txt`，`error` 为「合入冲突，用户的文件没有被改动；可自己修改、放弃，或用 resolve_conflicts_of 再委派解决。」；`note.txt` 保持你手动改后的内容，不出现冲突标记。
- **收回**：子运行中让主 Agent `reclaim` 并给出原因。
  - (a) 子 journal 有回传事实 `cancelled=true`；副本内容不合入用户文件。
  - (b) 工具结果 `code=RECLAIMED`；内容页状态 `已收回`，显示 `收回原因：<reason>`。
  - (c) 进程表里没有该子会话残留的 Worker（进程查询见 crash-recovery）。
  - (d) 再收回一次 → `ALREADY_RECLAIMED`；给一个不是本会话委派的 id → `UNKNOWN_CHILD`（「不是当前会话委派的子 Agent」）；子 Agent 自己调 `reclaim` → `RECLAIM_NOT_ALLOWED`（「子 Agent 不能收回其他子 Agent」）。
- **子失败**：(b) 子以失败结束时内容页状态 `执行失败`，父最新一轮出现 Alert `子 Agent 执行失败` 并显示失败文字；(c) 父会话本身不进入失败状态。

## Gotchas

- 没有 `sandbox.ready` 时副本无法创建，整条跳过。
- 模型可能不委派；通过条件是 `delegate` 工具真实出现在步骤里。
- `已派出` 只表示 `delegate` 调用本身成功（子已开工），不表示子任务完成；完成看内容页状态和 `交回结论`。
- 内容页与 `文件改动`、错误详情共用同一个右侧页面，一次只开一个。
- 历史 live 状态：通过（2026-10-12，main@1a4790a，当时界面为独立的「子 Agent 观察面板」）。2026-10-18 按源码改写为 `子 Agent` 内容页与新状态/结果码，未重新 live 验证。
