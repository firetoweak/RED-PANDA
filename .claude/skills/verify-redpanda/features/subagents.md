# 子 Agent

主 Agent 用 `delegate` 派出子任务，子 Agent 在从 Agent 分支切出的 worktree 里干活，`report` 交回结论；主 Agent 可用 `instruct_subagent` 调整执行中的任务、`reclaim` 收回、`compare_subagent` 看差异、`merge_subagent` 合进自己的分支。来源：`redpanda/assistant/subagent/subagent.py`、`redpanda/assistant/subagent/review.py`、`redpanda/tools/builtin/subagent_workspace.py`、`redpanda/assistant/host/agent_worktrees.py`（`create_child`/`compare_child`/`merge_child`）、`redpanda/assistant/conversations.py`（`observe_subagent`）、`SubagentCallCard.tsx`、`SubagentPanel.tsx`、`ExecutionProcess.tsx`、`ContentPage.tsx`、`Conversation.tsx`、`app.py` 的 `GET .../subagents/{command_id}`。

## Sub-features

- `delegate-card`：时间线出现 `delegate` 卡片，徽标 `排队中`/`运行中`/`已派出`（delegate 调用成功）/`失败`/`中断`/`待授权`/`已拒绝`，下方有任务文字与 `查看过程`（`失败`、`已拒绝` 时禁用）。
- `turn-marker`：执行过程顶部 `本轮子 Agent 任务`，徽标 `子 Agent · N`，每个任务一个按钮（aria-label `查看子 Agent 过程：<task>`）。
- `observe-panel`：点 `查看过程` 或上面的任务按钮，右侧打开内容页，标题与 aria-label 都是 `子 Agent`；状态 `正在创建`/`运行中`/`等待中`/`已交回`/`已收回`/`已结束，未报告`/`执行失败`/`连接中断`；有 `交回结论` 区、`收回原因：<reason>`、`回到最新`；关闭按钮 `关闭内容页`，拖宽分隔条 `调节内容页宽度`。
- `parent-hints`：父会话最新一轮在子仍未回收时显示 `子 Agent 执行中`；子以失败结束时显示 Alert `子 Agent 执行失败`。
- `isolation`：子 Agent 从 Agent 分支上已提交的工作切出自己的分支，改动写在 `$REDPANDA_HOME/state/coding_bot/<项目>/children/<子>`，看不到 Agent 未提交的修改，也不动 Agent 工作树。
- `compare-merge`：`compare_subagent` 先列文件再给 diff；`merge_subagent` 不需要授权、必须单独调用，把子分支合进 Agent 自己的分支；冲突返回 `MERGE_CONFLICT`，Agent 分支不变。
- `reclaim`：`reclaim` 结果码 `RECLAIMED`/`ALREADY_RECLAIMED`/`UNKNOWN_CHILD`/`RECLAIM_NOT_ALLOWED`。
- `instruct`：`instruct_subagent(child_session_id, text)` 给自己尚未结束的子任务追加指令；打断旧思考后沿用同一任务继续，结果码 `SUBAGENT_INSTRUCTED`/`CHILD_FINISHED`/`UNKNOWN_CHILD`/`INSTRUCT_NOT_ALLOWED`。
- `reply-parent`：子用 `reply_parent(text)` 回答父的询问或说明阶段进展，送达后继续任务；父收到 `subagent.reply`，它不表示最终交回。结果码 `REPLY_SENT`/`REPLY_NOT_ALLOWED`/`CHILD_FINISHED`，文本参数严格校验。
- `sidebar-wait`：有活跃子任务时，主会话侧栏状态圆点为 `干活`；等待提示 `子 Agent 执行中` 显示在主会话正文中。

## How to get to it (user POV)

- 在名册里的 Agent 会话里发 `请委派一个子 Agent 在 note.txt 末尾追加一行 child，完成后比较并合并它的修改。`（`note.txt` 须已提交在 Agent 分支上）

## Driving it with the browser

- **派出与观察**：
  - (a) `GET /api/sessions/<父 id>/subagents/<command_id>` 返回 200 与子会话过程；内容页里的步骤与之一致。
  - (b) 卡片徽标走到 `已派出`；子运行期间父最新一轮显示 `子 Agent 执行中`；子交回后内容页状态 `已交回`，`交回结论` 区显示摘要。
  - (c) 关闭内容页（`关闭内容页`）不影响子的运行；重新用 `查看子 Agent 过程：<task>` 打开，内容一致。
  - (d) `GET .../subagents/不存在` → 404，body `{"detail":"委派不存在：不存在"}`；面板读取失败时显示 `无法读取子任务过程`。
- **隔离**：合并前 (a) Agent 工作树的 `note.txt` 不含 `child`；`$REDPANDA_HOME/state/coding_bot/<项目>/children/<子>` 下的 `note.txt` 含 `child`。(c) 合并前 Agent 工作树与人的 `$SCRATCH_WORKSPACE` 不变。(b) `compare_subagent` 结果只列 `note.txt`。(d) 子 Agent 不能执行命令；任务里要求它跑命令 → 如实报告做不到。
- **合并**：`merge_subagent` 直接执行，不进入待处理条。
  - (a) Agent 工作树的 `note.txt` 末尾多一行 `child`，原有内容保留；`git log` 在 Agent 分支上多出合并结果；结果 `code=SUBAGENT_MERGED`，`changed_count` 与实际改动文件数一致。
  - (b) 卡片不出现 `待授权`。
  - (c) 人的 `$SCRATCH_WORKSPACE` 与集成分支不变。
  - (d) 合并前让 Agent 在自己分支上改 `note.txt` 末尾同一处并提交，制造冲突 → 结果 `ok=false`、`code=MERGE_CONFLICT`、`data.conflicts` 列出 `note.txt`，`error` 为「合入冲突，你的分支没有被改动；可自己修改、放弃，或用 resolve_conflicts_of 再委派解决。」；Agent 分支与工作树不出现冲突标记。Agent 工作树有会被覆盖的未提交文件或未完成合并时，返回 `MERGE_BLOCKED` / `MERGE_IN_PROGRESS`。
- **收回**：子运行中让主 Agent `reclaim` 并给出原因。
  - (a) 子 journal 有回传事实 `cancelled=true`；子分支内容不合入 Agent 分支。
  - (b) 工具结果 `code=RECLAIMED`；内容页状态 `已收回`，显示 `收回原因：<reason>`。
  - (c) 进程表里没有该子会话残留的 Worker（进程查询见 crash-recovery）。
  - (d) 再收回一次 → `ALREADY_RECLAIMED`；给一个不是本会话委派的 id → `UNKNOWN_CHILD`（「不是当前会话委派的子 Agent」）；子 Agent 自己调 `reclaim` → `RECLAIM_NOT_ALLOWED`（「子 Agent 不能收回其他子 Agent」）。
- **追加指令**：先委派一项较长的只读调查，子仍运行时，从父输入框 `打断并发送` 要求用 `instruct_subagent` 调整为「只通过 report 交回 CHILD_REDIRECT_OK」。
  - (a) 父实际调用 `instruct_subagent`，目标与原 `delegate` 的子 id 一致；GET 观察接口结果为 `reported=true` 且 summary 含新标记，`cancelled=false`。
  - (b) 子内容页显示 `已交回` 和新结论，父收到结论后回复新标记。
  - (c) 没有第二次 delegate 或 reclaim；子原历史保留，新指令是 `subagent.instruction` 事实，不能伪装成用户消息；一次委派仍只交回一次。
  - (d) 已交回或收回的子拒绝新指令（`CHILD_FINISHED`）；重复投递不再打断新工作；报告正在提交时保留原结论，不接续新方向。后两项由进程测试核对。
- **子失败**：(b) 子以失败结束时内容页状态 `执行失败`，父最新一轮出现 Alert `子 Agent 执行失败` 并显示失败文字；(c) 父会话本身不进入失败状态。
- **自主询问进度**：在全新 Bot 中用普通语言要求把一项较长任务交给子助手。确认真实委派且子仍在运行后，从父输入框只发「做到什么程度了？」；初始任务和追问都不能指定工具名、回复协议或测试标记。
  - (a) 父自主调用 `instruct_subagent` 向原子任务询问实际进度；子用 `reply_parent` 答复后，父收到对应 `subagent.reply` 并据此回答用户。
  - (b) 核对答复内容与先后顺序，区分委派目标、模型计划和实际完成的工作。未询问就复述目标、只说等待报告，或直接从文件推测子进展，不计作这项通过。
  - (c) 阶段回复时子仍活跃，不能重派或收回，也不能把阶段回复当最终交回。另一个新 Bot 可复测不同自然问法；一次通过不等于跨模型稳定性保证。
- **插话问答链路**：长任务执行中，显式要求父用 `instruct_subagent` 问当前进展、子用 `reply_parent` 答复后继续。这里只证明通信链路，不证明模型自主选择工具。
  - (a) 子历史出现成功的 `reply_parent`；父历史有对应 `subagent.reply`，含原子 id 与答复。此时观察 GET 的 result 仍为 null，父 `has_active_subagents=true`。
  - (b) 父收到阶段答复后可转述进展；页面仍显示 `子 Agent 执行中`。再问一次可收到另一条答复，最终才显示 `已交回`。
  - (c) 阶段性答复不触发回收、不伪装为用户消息；原 delegate 不变，最终 report 与 return 各一条。
  - (d) 子在送达确认前退出，恢复核对同一回复，不重复投递、不误报任务失败；已结束时拒绝新的阶段回复，旧回复仍可确认。进程测试核对这些边界。

## Gotchas

- 只在 Agent 会话里验证本条。普通会话（终端入口）的子 Agent 只读调查：只读父的真实目录，没有写工具，也没有 `compare_subagent`/`merge_subagent`。
- 子分支跟随 Agent 保留，不随交回删除。
- 模型可能不委派；通过条件是 `delegate` 工具真实出现在步骤里。
- `已派出` 只表示 `delegate` 调用本身成功（子已开工），不表示子任务完成；完成看内容页状态和 `交回结论`。
- 内容页与 `文件改动`、错误详情共用同一个右侧页面，一次只开一个。
- 历史 live 状态：通过（2026-10-12，main@1a4790a，当时界面为独立的「子 Agent 观察面板」）。2026-10-18 按源码改写为 `子 Agent` 内容页与新状态/结果码，未重新 live 验证。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：同一步派出两个 delegate，观察页已交回并显示 write/read/report 与交回结论，compare_subagent/merge_subagent 及父目录成果通过；收回、冲突、子失败未重验。
- 2026-10-22 插话控制增量：隔离 Web、Windows Chrome、真实 DeepSeek；子任务运行时插话保留它，父先回复且子继续执行；随后另派任务，在其运行中要求父 `reclaim`，观察页显示已收回与指定原因，GET 的 `cancelled=true`。自然结束与收回的首次竞态不计作收回通过；选择性收回及保留任务正常交回另由进程测试验证。未重验分支改动与合并、冲突、子失败；证据见 `.tools/interrupt-adjust-evidence/`。
- 2026-10-22 追加指令增量：隔离 home、Windows Chrome 1440×900、真实 DeepSeek；排序长文子任务执行中，父实际调用 `instruct_subagent`，原子任务中断旧思考并通过 report 交回 `CHILD_REDIRECT_OK`。GET 与 journal 确认同一子 id、一条新指令、一次决策取消、没有用户消息，最终 `reported=true`、`cancelled=false`；父未重新委派或收回。进程测试另验父在接纳确认前退出后的恢复幂等、报告提交竞态和已结束拒绝。未真实驱动代码项目写入/MCP 长调用/Linux；代码项目分支与原有停止等由进程回归核对。证据在 `.tools/subagent-instruction-evidence/`。
- 2026-10-22 插话问答链路增量：隔离 home、Windows Chrome 1440×900、真实 DeepSeek；测试提示显式指定工具，父连续两次用 `instruct_subagent` 问进度，子两次 `reply_parent` 返回 `PROGRESS_ONE`/`PROGRESS_TWO`，父自动接到并转述，此时仍有活跃子任务。随后父调整同一个子任务，最终正常 report `DIALOGUE_FINAL_OK`。Journal 核对一次委派、三次插话、两条阶段答复、一条最终交回、零子会话用户消息，没有收回或重派。只证明链路，不证明自然问法下模型自主选择。子在回复确认前退出的恢复、旧回复在终局后的确认与新回复拒绝由进程测试覆盖；未 live 验证代码项目写入、MCP 长调用与 Linux，不评价本次算法写作内容的正确性。证据在 `.tools/subagent-dialogue-evidence/`。
- 2026-10-22 自主询问增量：隔离 home、Windows Chrome 1440×900、真实 deepseek-flash。原版本只问「做到什么程度了？」失败：父查文件并称没有子 id；保留可寻址 id、但仅说「需要了解时询问」仍失败，请求核对确认模型拿到了工具与 id，却选择等待。明确用户问进度且已有信息不足时应先询问后，在两个全新 Bot 中用「做到什么程度了？」和「现在进展怎么样，有卡住的地方吗？」均自主调用原子任务的 `instruct_subagent`，子 `reply_parent`，父收到后转述。数据库教程例中子随后实际写文件并继续修改，没有收回或重派。排序教程例的初次问答通过，但后续父自行要求分批交稿时，子用了普通文字而非回复工具，未 report 就结束，父随后重派；该后续流程不计通过。保留三次失败与两个最终样本，不据此宣称统计可靠性、跨模型表现、教程内容正确或任务完整交付通过。证据在 `.tools/subagent-natural-evidence/`。
