# 子 Agent 协作

父用 `delegate` 委派、`instruct_subagent` 交流或调整、`list_subagents` 看实际运行状态、`reclaim` 明确结束协作。子用普通回复对父说话，没有 `report` 或 `reply_parent` 工具；普通回复、停止输出和等待消息都不结束协作。

来源：`assistant/subagent/subagent.py`、`host/supervisor.py`、`host/worker.py`、`assembly.py`、`conversations.py`、`SubagentPanel.tsx`、`SubagentCallCard.tsx`、`Conversation.tsx`。

## Sub-features

- 委派、普通回复与持续追问。
- 状态查询、显式结束与父停止。
- 共享文件、完整执行、命令授权与回收。

## How to get to it (user POV)

隔离 home、空名册、独立端口；Doctor 必须通过且 `live_model_ready=true`。创建 Bot 后从消息框驱动，初始任务与追问都不指定工具名或回复协议，不注入测试标记。确认真实 `delegate`，模型自行完成不计作委派通过。

自然任务示例：请交给一位子助手写技术说明，分批审阅，先交三节提纲和第一节，其他章节等用户确认后再写；父审阅后汇报并保留协作。

## Driving it with the browser

### 普通回复、等待与继续

- 第一批出现：父收到 `subagent.reply`，正文与子普通回复一致。子历史没有 `report` 或 `reply_parent` 调用，实际模型工具列表也没有它们。
- 第一批之后：观察 `GET /api/sessions/<父 id>/subagents/<delegate command id>` 的 `result=null`，子空闲时页面为 `等待中`。父的 `has_open_subagents=true`；父可显示中性的「有未结束的子 Agent 协作」，不能据此显示父仍在干活。
- 只用普通语言追加修改和第二批要求：父向原 child id 发 `instruct_subagent`，子保留第一批历史并普通回复第二批。没有重派、自动结束或把输入伪装成子会话用户消息。
- 父可以根据收到的内容主动要求修正，普通回复仍能多轮往返。结束用户一轮回复不使子无法继续。
- 子消息送达后确认丢失：原 `deliver` 以同一身份恢复，父只收到一条消息，不因未知发送误判失败。此项由进程测试注入退出验证。
- 父已空闲、子仍在处理时重启 Web：不重新选中父，子也要继续并按原身份回传；父只在新消息到达后再决策。等待消息的协作不自动结束，再次重启不凭空增加父子模型决策或重复回复；已停止执行的父不自动恢复子任务。

### 状态与结束

- 自然询问「子现在是在执行还是等消息，查一下状态」，父自主调用 `list_subagents`。返回状态与实际执行一致：running / waiting / closed / failed / creating。状态不表示语义进度，不从完整过程生成摘要。
- 进度内容仍直接问子，父根据普通答复向用户解释，不能拿委派目标或计划当进展。
- 用户说现有内容已经够用、其余不再需要时，父自行用 `reclaim` 结束。子内容页显示 `协作已结束`；提供原因时另显示 `结束原因：<reason>`，GET 的 result 含 failure / reason；不是一律取消或失败。
- 明确结束后：父 `has_open_subagents=false`，只有一条子结束事实和一条父关闭通知；历史与文件保留，新指令返回 `CHILD_FINISHED`，重复结束返回 `ALREADY_RECLAIMED`。已送达回复的旧确认仍可恢复，不接纳结束后的新回复。
- 人点父的 `停止` 时结束全部未结束协作，包括正在等待消息的子；当前轮取消不阻止后续新消息处理。父只是在本轮空闲时则不关闭子。

### 共享文件与执行

- 父子默认使用同一工作区，未提交文件直接可见；可通过 cwd 指定父已有可访问目录，不创建工作树或提交。子与父文件访问和授权相同，不能再次委派。
- 关闭自动放行，让父交给子生成文件并执行核对脚本。文件写入和命令均须在父对话呈现待处理条，参数可展开，名册提醒属于父；允许或拒绝只作用于对应子的实际调用。
- 子后台命令启动后，完成通知回到子继续判断；父可处理人的新问题。子等待消息或报告进展不结束协作，不能凭普通回复判断执行资源已经结束。
- 父验收后必须 reclaim；确认子命令终止、共享文件保留、has_open_subagents 清除，旧完成通知不再次唤醒子。资源释放失败应暴露错误。
- 没有 compare_subagent、merge_subagent 或自动提交；文件分工、分支与并行顺序由父安排。并行地图见 subagents-parallel。

### 页面与证据

委派卡片为 `已派出`，不表示完成；用 `查看子 Agent 过程：<task>` 打开右侧只读内容页。打开、关闭不选择或唤醒子。观察不存在的委派返回 404。

保存 snapshot.txt / page.png / state.json / child-state.json / note.txt；多轮保留第一批等待与最终结束两个现场，并从 journal 核对普通回复正文、稳定身份、父处理来源与结束先后。完整日志和一次性脚本只放 `.tools/`。

## Gotchas

旧设计曾通过显式工具链路，但自然追问出现不主动询问，分批正文还出现写出却未送达、空闲被提前结束。旧证据在 `.tools/subagent-natural-evidence/`，不代表当前普通回复契约已验证。

2026-10-22 当前增量：隔离 Web、Windows Chrome 1440×900、真实 deepseek-flash。无工具名提示地委派第一批，子普通正文送父，父审阅后保留协作；父自行查询到 running。自然追加要求后同一个子交第二批，父又自主要求改正「都会」为「都可能」，子第三次普通回复后仍等待。用户说现有内容够用、不写第三节，父自主结束；页面为「协作已结束」。一次委派、多轮普通回复、明确结束由 journal 复核。证据在 `.tools/subagent-collaboration-evidence/`。内容事实正确性、跨模型稳定性、Linux 与长 MCP 接续未在本增量验证；项目写入、等待时验收合入、停止和恢复另由进程回归核对。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：无项目子助手初稿、同一子追问与多批普通回复、父显式 reclaim 通过；过程页可观察，普通回复不自动结束协作。

- 2026-10-22 启动恢复增量：隔离 Windows Web、Chrome、真实 DeepSeek；从消息框委派后，在子模型请求处模拟延迟，父已等待且 `should_wake=false`。实际停止 Web 并用正常入口重启，未重新选中父前已收到原子回复。再次重启，父、子 Journal 和决策数均不变。暂停边界另由进程回归验证；未覆盖 Linux、项目子 Git 操作和全部传输中断。证据：`.tools/recovery-fixes/startup-web-evidence/`。

2026-10-26 增量：Windows Chrome 1920×1031、隔离 home/8884 端口、受控本地协议模型。无项目 Bot 委派子写共享文件，父对话展开并允许写入及后台命令；真实脚本读取共享文件，父在其运行期间回复新问题；自然要求结束后父实际 reclaim，退出记录为 stopped，延迟文件未生成，共享文件保留、协作提示清除，浏览器无错误。未调用真实供应商，未重验窄屏、TUI、浏览器 MCP 资源释放及其他操作系统。证据 `.tools/shared-subagent-ui/evidence/`；原始状态与脚本留本地。
