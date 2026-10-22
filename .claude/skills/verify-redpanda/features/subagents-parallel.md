# 并行多个子 Agent

Agent 在同一次决策里发出多个 `delegate`，各子 Agent 在各自从 Agent 分支切出的 worktree 里并行修改；之后并发地验收、合并、收回。来源：`redpanda/assistant/subagent/subagent.py`（`DELEGATE_SCHEMA`、`CHILD_BUILTIN_TOOL_NAMES`、`record_interrupted_return`、`rehydrate`、`reclaim`、`resolve_conflicts_of` 校验）、`redpanda/assistant/subagent/review.py`、`redpanda/assistant/host/agent_worktrees.py`（`create_child`/`finish_child`/`compare_child`/`merge_child`）、`redpanda/tools/builtin/subagent_workspace.py`、`redpanda/assistant/host/supervisor.py`（`_cancel_turn`、`wait_quiescent`、`_running_children`）、`redpanda/assistant/host/session_store.py`、`SubagentCallCard.tsx`、`SubagentPanel.tsx`、`Conversation.tsx`。

源码事实（判据以此为准，2026-10-22 普通回复契约修订）：
- `delegate` 描述给模型的相关句子：互不依赖的任务可在同一次决策里各发一次 delegate 并行；普通回复逐个送父，验收完成或不再需要时父明确结束。
- Agent 的子 Agent 工具是显式白名单：`glob` `grep` `read_file` `read_content` `find_history` `load_skill` `read_skill_resource` `write_file` `apply_patch` `replace_all` `update_plan`。**没有 `execute_command`，也没有 `delegate`**。
- 子会话的 journal 在 `$REDPANDA_HOME/runtime_sessions/<sha256(session_id) 十六进制>/journal.sqlite`；子会话不出现在 Agent 名册（`GET /api/agents`）里。子 worktree 在 `state/coding_bot/<项目>/children/<子>`，分支从 Agent 分支上已提交的工作切出；兄弟之间没有共享队列。
- `merge_subagent`：不需要授权、必须单独调用，合进 Agent 自己的分支；冲突时 Agent 分支不变，返回 `MERGE_CONFLICT` 与 `data.conflicts`。`delegate` 的可选参数 `resolve_conflicts_of` 必须是本会话里已经结束协作的 delegate 调用 id，否则 `UNKNOWN_CONFLICT_SOURCE`（「解冲突只能引用当前会话已经结束协作、并且 project 相同的委派。」）。
- 父会话 `停止`（`cancel_turn`）会结束所有未结束协作，包括等待消息的子，原因 `用户中断父会话`。
- 子会话里有已开始但无结果的调用时，Worker 启动会以失败回传：「子 Agent 执行中断，存在未取得结果的操作。需要时重新 delegate。」；父最新一轮显示为 Alert `子 Agent 执行失败`。
- 源码里没有并发上限（`subagent/` 下无 Semaphore/limit），见 Gotchas。

## Sub-features

- `parallel-delegate`：一次决策发出 N 个 `delegate`，N 张卡片，子同时 `运行中`。
- `worktree-isolation`：各子 worktree 互相隔离，也与 Agent 工作树、人的工作区隔离。
- `concurrent-review`：多个子交回后分别 `compare_subagent`。
- `merge-conflict`：两个子改了同一文件，先合的成功，后合的返回 `MERGE_CONFLICT`。
- `conflict-redelegate`：带 `resolve_conflicts_of` 重新委派解决冲突。
- `concurrent-reclaim`：其中一个运行中被 `reclaim`，其他继续。
- `parent-control`：多个子运行中时，父会话 `停止`/服务重启的表现。
- `accounting`：正文 `有未结束的子 Agent 协作`、侧栏父自身执行状态圆点、`本轮子 Agent 任务`、上下文用量与计划的归属。

## How to get to it (user POV)

准备：Bot 已挂载一个 Git 项目，以下每次 `delegate` 都显式传该项目名 `project`；Bot 的项目分支上已提交 `a.txt`（`A0`）、`b.txt`（`B0`）、`shared.txt`（`S0`）。

```text
请在同一次决策里并行委派三个子 Agent，每次 delegate 都指定已挂载的项目名：
1. 把 a.txt 改成 A1；2. 把 b.txt 改成 B1；3. 把 shared.txt 改成 S-third。
等三个都交回后，先 compare 每一个，再逐个 merge_subagent。
```

冲突场景另发：`请并行委派两个子 Agent，都去改 shared.txt：一个改成 S-left，一个改成 S-right。交回后依次合并，冲突时告诉我冲突清单。`

## Driving it with the browser

- **并行派出**：
  - (a) `GET /api/sessions/<父 id>` 中同一步里有 3 个 `delegate` 调用，各有不同 `child_session_id`；`runtime_sessions/` 下多出 3 个以 sha256 命名的目录；`state/coding_bot/<项目>/children/` 下有 3 个 worktree；`GET /api/agents` 不多出子会话。
  - (b) 3 张卡片徽标 `已派出`，`本轮子 Agent 任务` 显示 `子 Agent · 3`；至少两个子的内容页同时为 `运行中`（截图时间戳为证）；父状态取决于父自身是否执行，正文显示 `有未结束的子 Agent 协作`；每个 `GET .../subagents/<command_id>` 都 200。
  - (c) 合并前 Agent 工作树的 `a.txt` `b.txt` `shared.txt` 字节与 mtime 不变。
  - (d) 任务里写「用 execute_command 跑 ls」→ 子没有该工具，应如实报告做不到，子过程里不出现命令执行；要求子再 `delegate` → 同样不可用。
- **worktree 隔离**：(a) 子 1 worktree 里 `a.txt`=`A1`，`b.txt` 仍为 `B0`。(b) 各子内容页只显示自己的过程。(d) 见并发验收的路径校验。
- **并发验收**：(a) 每个子的 `compare_subagent` 只列它自己改过的文件（子 1 只有 `a.txt`）。(b) 结果先列文件，指定 `paths` 才给 diff。(c) 验收不改 Agent 工作树。(d) `paths` 传 `../x` 或绝对路径 → `paths 必须是工作树内的相对路径`。
- **合并（无冲突）**：每次 `merge_subagent` 都是单独一批，直接执行、不进入待处理条。(a) 依次合入后 Agent 工作树为 `A1`/`B1`/`S-third`；每个结果 `code=SUBAGENT_MERGED`，`changed_count` 与实际改动文件数一致。(b) 卡片不出现 `待授权`。(c) 人的工作区与集成分支不变。(d) 同一子再合一次 → `changed_count=0`，不重复改动。
- **合并冲突**：(a) 先合的使 `shared.txt`=`S-left`；后合的 `shared.txt` **仍为 `S-left`**，Agent 工作树里没有冲突标记，其他文件不变。(b) 结果 `code=MERGE_CONFLICT`，`data.conflicts` 含 `shared.txt`，`error` 为「合入冲突，你的分支没有被改动；可自己修改、放弃，或用 resolve_conflicts_of 再委派解决。」。(c) 不得出现「后合覆盖先合」。(d) 带 `resolve_conflicts_of=<冲突那次 delegate 的调用 id>` 重派 → 新子的 worktree 里 `shared.txt` 带冲突标记开工，解决后合并得到唯一结果；`resolve_conflicts_of` 填一个不存在或尚未结束协作的 id → `UNKNOWN_CONFLICT_SOURCE`。
- **并发收回**：三个运行中让父 `reclaim` 子 2。(a) 子 2 journal 有结束事实；其他两个继续并正常交回。(b) 结果 `RECLAIMED`，子 2 内容页 `协作已结束` 与 `结束原因：<reason>`。(c) 子 2 的修改不进入 Agent 分支。(d) 再收回子 2 → `ALREADY_RECLAIMED`。
- **父会话 `停止`**：三个运行中点输入框的 `停止`。(a) 每个子 journal 有结束事实，原因 `用户中断父会话`；之后各子事件数不再增长；`GET /api/sessions/<父 id>` 的 `has_open_subagents` 为 false。(b) `cancel` 返回 200；各子内容页 `协作已结束`，显示 `结束原因：用户中断父会话`。(c) 子 worktree 与分支保留可查看，不自动合入、不撤销。(d) 停止后子仍在跑或仍交回结论 → 记为问题。
- **服务重启**：三个运行中杀服务后同一 home 重启（命令见 crash-recovery）。(a) 子不会自己恢复：只有父会话走恢复（点 `继续` 或发新消息）时 `rehydrate` 才认回并推进未回收的子；子会话里已开始但无结果的调用让该子以失败回传「子 Agent 执行中断，存在未取得结果的操作。需要时重新 delegate。」。(b) 父最新一轮出现 Alert `子 Agent 执行失败`。(c) 不重复执行已完成的子工具调用（比较子 worktree 内容与 journal 调用次数）。(d) 父只在等子回传时不会被自动 execution_stopped，界面也可能没有 `继续`；若此时子始终不推进、只能靠发新消息唤醒，记为疑似问题并写明实际表现。
- **计量**：(a) 父的 `上下文用量` 不包含子的过程文本，也不要求累计值单调增长；子计划不覆盖父的 `当前计划`，子 `update_plan` 调用可在子过程记录中核对，观察页不另展示计划面板。(b) `本轮子 Agent 任务` 数等于 delegate 数。(c) 子思考和工具过程不进父转录，子普通回复进入父上下文。(d) 父转录里出现子思考或完整工具过程 → 记为问题。

## Gotchas

- 疑似问题：无并发上限。`subagent/` 中没有限制并行数量的代码；一次委派很多个（如 10 个）会同时起 10 个 Worker 与 10 个 worktree。验证时观察内存、磁盘与模型并发 429。
- 父会话停止只收回子任务，子过程与子分支保留可查看，不自动撤销文件变化、不自动合入。
- 模型可能串行委派而非并行；通过条件是同一步出现多个 `delegate` 且子运行时间重叠。
- 需要 `live_model_ready`。文件修改、worktree 与合并场景要求 Bot 已挂项目且 delegate 指定 project；不指定项目的委派也可并行只读调查，但不创建独立子 worktree，子不能写文件、执行命令或再委派，可经 console-chat 验证此入口。
- 历史 live 状态：部分（2026-10-12，修复后）：父中断收回子任务、活跃子任务期间回滚收尾由 `tests/assistant/test_supervisor.py` 覆盖；并行委派、副本隔离、并发合并冲突的界面流程未验。2026-10-18 按源码改写，未重新 live 验证。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：journal 确认同一决策步骤两个 delegate，独立子工作树创建 child-a/child-b，分别比较合并后父目录包含两文件；并发冲突、收回与父停止传播未重验。

- 当前普通回复与结束语义的 live 范围见 subagents。旧并行修改/合并实测不作为本次新回复与结束语义通过的证据；本次进程回归核对项目写入、等待时比较/合入与父明确结束。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：同一决策两个带 project 的 delegate，各自写文件，compare、逐次 merge、reclaim 后父目录含两文件；并发冲突与停止传播未重验。
