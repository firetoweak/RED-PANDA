# 并行多个子 Agent

主 Agent 在同一次决策里发出多个 `delegate`，各子 Agent 在自己的文件副本里并行修改；之后并发地验收、合并、收回。来源：`redpanda/assistant/subagent/subagent.py`（`delegate` 描述、`CHILD_BUILTIN_TOOL_NAMES`、`record_interrupted_return`、`rehydrate`）、`redpanda/sandbox/files/children.py`（`ChildFiles.create/finish/compare/merge/discard`）、`redpanda/tools/builtin/subagent_workspace.py`、`redpanda/assistant/host/supervisor.py`（`_reclaim_child`、`archive`、`wait_quiescent`、`_children_still_running`）、`SubagentCallCard.tsx`、`SubagentPanel.tsx`。

源码事实（判据以此为准）：
- `delegate` 描述明确允许「同一次决策里各发一次 delegate，子 Agent 之间并行推进」，并警告「并行的子 Agent 改同一批文件，合入时会冲突」。
- 子 Agent 能用的工具是显式白名单：`glob` `grep` `read_file` `read_content` `find_history` `get_changes` `load_skill` `read_skill_resource` `write_file` `apply_patch` `replace_all` `restore_workspace` `update_plan` `report`。**没有 `execute_command`，也没有 `delegate`**（不能递归委派）。
- 每个子的副本：`$REDPANDA_HOME/state/subagent_worktrees/<key>`；基线/结果：`state/file_exchanges/<key>/baseline.json`、`result.json`；`<key>` 由父、子 id 派生（`_child_key`）。创建锁只按同一委派串行（注释：「Siblings have no shared queue」）。
- `merge_subagent`：需要授权、`exclusive_batch=True`（必须单独调用）；冲突时「不改动任何文件，只返回冲突清单」。`delegate` 的可选参数（`resolve_conflicts_of`）让新子从带冲突标记的工作树开工。
- 源码里**没有找到并发上限**（`subagent/` 下无 Semaphore/limit），见 Gotchas。

## Sub-features

- `parallel-delegate`：一次决策发出 N 个 `delegate`，N 张卡片同时 `运行中`。
- `overlay-isolation`：各子副本互相隔离，也与用户工作区隔离。
- `concurrent-review`：多个子交回后分别 `compare_subagent`。
- `merge-conflict`：两个子改了同一文件，先合的成功，后合的返回冲突清单。
- `conflict-redelegate`：带 `resolve_conflicts_of` 重新委派解决冲突。
- `concurrent-reclaim`：其中一个运行中被 `reclaim`，其他继续。
- `parent-control`：多个子运行中时，父会话 `中断`/`暂停`/归档/回滚的表现。
- `accounting`：侧栏 `等待子 Agent`、`本轮子 Agent 任务`、上下文用量与计划的归属。

## How to get to it (user POV)

准备：工作区里有 `a.txt`（`A0`）、`b.txt`（`B0`）、`shared.txt`（`S0`）。自动放行关闭（以便看到合并授权）。

```text
请在同一次决策里并行委派三个子 Agent：
1. 把 a.txt 改成 A1；2. 把 b.txt 改成 B1；3. 把 shared.txt 改成 S-third。
等三个都交回后，先 compare 每一个，再逐个 merge_subagent。
```

冲突场景另发：`请并行委派两个子 Agent，都去改 shared.txt：一个改成 S-left，一个改成 S-right。交回后依次合并，冲突时告诉我冲突清单。`

## Driving it with the browser

- **并行派出**：
  - (a) `GET /api/sessions/<父 id>` 中同一步里有 3 个 `delegate` 调用，各有不同 `child_session_id`；`$REDPANDA_HOME/runtime_sessions/` 下多出 3 个子会话目录；`state/subagent_worktrees/` 下有 3 个副本。
  - (b) 时间线出现 3 张卡片，至少两张同时处于 `运行中`（截图时间戳为证）；父侧栏为 `等待子 Agent`；`GET .../subagents/<command_id>` 对每个都返回 200。
  - (c) 合并前用户工作区 `a.txt` `b.txt` `shared.txt` 字节不变、mtime 不变。
  - (d) 让子 Agent 执行命令（任务里写「用 execute_command 跑 ls」）→ 子没有该工具，应如实报告做不到，不得出现命令执行；要求子再 `delegate` → 同样不可用。
- **副本隔离**：(a) 子 1 副本里 `a.txt`=`A1`，但它的 `b.txt` 仍为 `B0`（看不到子 2 的修改）。(c) 一个子的 `restore_workspace` 只影响它自己的副本，不影响兄弟副本与用户文件。
- **并发验收**：(a) 每个子的 `compare_subagent` 只列出它自己改过的文件（子 1 只有 `a.txt`）。(d) `paths` 传 `../x` 或绝对路径 → `paths 必须是工作树内的相对路径`。
- **合并（无冲突）**：每次 `merge_subagent` 都出现 `待授权` 且单独成批。(a) 依次 `允许` 后工作区为 `A1`/`B1`/`S-third`；结果的 `changed_count` 与实际改动文件数一致。(d) 拒绝其中一个 → 该子的文件不合入，其他已合入的保持。
- **合并冲突**：(a) 先合的子使 `shared.txt`=`S-left`；后合的返回冲突清单，`shared.txt` **仍为 `S-left`**，没有冲突标记写入用户文件，其他文件也不变。(b) 工具结果列出 `shared.txt` 为冲突。(c) 不得出现「后合覆盖先合」。(d) 带 `resolve_conflicts_of` 重派 → 新子的副本里 `shared.txt` 带冲突标记开工；解决后合并得到唯一结果。
- **并发收回**：三个运行中让父 `reclaim` 子 2。(a) 子 2 journal 有回传事实 `cancelled=true`；其他两个继续并正常交回。(b) 子 2 面板 `已收回`。(c) 子 2 的修改不进入用户工作区。`ps` 无子 2 的 Worker 进程。
- **父会话 `中断`**：三个运行中点 `中断`。记录：子是否被一起停下。源码里 `archive` 会逐个 `_reclaim_child`（原因 `父会话归档`）并 `discard` 副本，但 `cancel_turn` 路径没有看到对子的收回逻辑——若中断后子仍在跑、仍回传结论，记为疑似问题而非通过。
- **归档父会话**：(a) 所有子 journal 有 `cancelled` 回传；`state/subagent_worktrees/`、`state/file_exchanges/`、`state/workspace_views/` 中对应目录被删除。(c) 用户工作区不变。
- **父会话回滚（file-rewind）且子仍在跑**：源码 `restart_from_step` 先 `cancel_turn` 再 `wait_quiescent`，后者会等待未回传的子（`_children_still_running`）。预期：HTTP 请求可能一直挂到子结束。记录耗时；若超过子任务时长仍不返回或从不返回，记为问题。
- **服务重启**：三个运行中 `kill` 服务后同一 home 重启。(a) `rehydrate` 应认回父子关系并唤醒未回收的子；开始了但无结果的子工具调用会以失败回传：`子 Agent 执行被中断：以下工具调用已经开始但没有返回结果，不知道是否生效，也没有自动重试。`。(c) 不得重复执行已完成的子工具调用（比较副本文件内容与 journal 调用次数）。
- **计量**：(a) 父的 `上下文用量` 只因 `report` 摘要增长，不包含子的过程文本（`routed_sink` 注释：子正文只路由到观察端口）。各子的 `update_plan` 只出现在子面板，不出现在父的 `当前计划`。(b) `本轮子 Agent 任务` 数等于 delegate 数。

## Gotchas

- **疑似问题：无并发上限。** `subagent/` 中没有限制并行数量的代码；让模型一次委派很多个（如 10 个）会同时起 10 个 Worker 进程与 10 份副本（全量 materialize）。验证时观察内存、磁盘与模型并发 429。
- 父会话中断会收回未结束的子任务（复用 Host 收回流程），子任务过程与成果保留可查看，不自动撤销文件变化、不自动合入。
- 活跃子任务期间回滚父会话：等待子任务收尾后完成回滚，子任务过程与成果保留（自动化通过）。
- 模型可能串行委派而非并行；通过条件是同一步出现多个 `delegate` 且卡片时间重叠。
- 需要 `sandbox.ready` 与 `live_model_ready`。

## Live status
- 部分（2026-10-12，修复后）：父中断收回子任务、活跃子任务期间回滚收尾（自动化通过，`tests/assistant/test_supervisor.py`）
- 未验（待真实 UI）：并行委派、副本隔离、并发合并冲突的界面流程
- 更严格成功标准：父会话中断后各子 Agent 事件数停止增长、`has_active_subagents` 变为 false——自动化已覆盖，UI 待人工复跑
