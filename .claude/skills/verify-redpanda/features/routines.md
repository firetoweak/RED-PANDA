# 例行任务

长期 Agent 用 `save_routine` / `set_routine_state` / `list_routines` 管理自己的例行任务；人在该 Agent 的 `Agent 资料`（点页头名字打开）的 `例行任务` 一节里查看、暂停、恢复与删除。来源：`redpanda/automation/routines.py`（`RoutineStore`、`RoutineClock`、状态 `active`/`paused`/`paused_by_user`/`paused_unwatched`/`deleted`、事实 `automation.routine_due`、表 `routine_fires`）、`redpanda/automation/routine_tool.py`、`redpanda/assistant/host/supervisor.py`（`list_routines`、`routine_fires`、`set_routine_state_by_user`、`schedule_changed`）、`redpanda/channels/web/channel.py`、`web/src/features/conversation/RoutineList.tsx`。存储 `$REDPANDA_HOME/state/routines.sqlite`。

## Sub-features

- `list`：`Agent 资料` 侧栏（`aside`，`aria-label="Agent 资料"`）里的 `例行任务` 一节，不折叠。每项是按钮 `查看 <名字>`，显示名字、状态（`运行中` / `Agent 暂停` / `人暂停` / `无人查看已暂停`）、可读时间安排和 `上次 <时间> · 下次 <时间>`。可读安排例如 `@every 10m` → `每 10 分钟`、`0 2 * * *` → `每天 02:00`；译不出的时间表照原样显示。还没有跑过时上次是 `尚未运行`，暂停因而没有下一次时下次是 `未安排`。没有例行任务时这一节显示灰色 `无例行任务`。对话的输入框上方不再显示例行任务。
- `pause`：运行中的一项有开关 `暂停 <名字>`，关掉后状态变 `人暂停`。
- `resume`：暂停态（`Agent 暂停`、`人暂停` 或 `无人查看已暂停`）的一项有开关 `恢复 <名字>`，打开后回到 `运行中` 并重新排下一次。
- `delete`：每项有 `删除 <名字>`；点后出现 `确认删除 <名字>` 与 `保留`，确认后这一项从列表消失。删除不可恢复。
- `detail`：点 `查看 <名字>` 进入该项。能看到完整意图、可读时间安排（和原文，当两者不同）、上次与下次，以及 `执行记录`。记录按触发时间倒序，每条有 `计划 <时间> · 触发 <时间>`，以及 `已送达` 或 `未送达`。没有记录时显示 `还没有执行过`。`返回例行任务` 回到列表。
- `jump`：已送达、并且这次触发已经出现在对话时间线上的记录，有按钮 `在时间线中查看 <触发时间>`。点它滚到 Bot 由此开始的那一轮并短暂高亮。还没送达，或回复还没写入时间线时，没有这个按钮。
- `user-pause-sticks`：人暂停的一项，Agent 调 `set_routine_state` 恢复或删除会得到 `code=PAUSED_BY_USER`，只能由人恢复或删除。
- `unwatched-pause`：无人查看询问及暂停见 [inbox.md](./inbox.md)；`paused_unwatched` 显示 `无人查看已暂停`，Agent 调 `set_routine_state` 恢复或删除会得到 `PAUSED_UNWATCHED`，只能由人处理。
- `refresh`：Agent 自己新建、改写或暂停例行任务、到点触发后，SSE `schedule_changed` 让列表和打开中的执行记录重读，不需要刷新页面。

## How to get to it (user POV)

- 打开一个 Agent 的会话，发 `请用 save_routine 建一个例行任务，名字「检查 PR」，每 10 分钟一次（@every 10m），intent 写「看看有没有新评审」，然后回复 SAVED。`
- 回复后点页头名字打开 `Agent 资料`，`例行任务` 一节出现 `检查 PR`。宽屏是标题为 `Bot 资料` 的侧栏；1180px 及以下（73.75em，默认字号）是右侧抽屉（`role="dialog"`，名称 `Agent 资料`），用 `关闭 Agent 资料` 或 Escape 关闭。
- 要看执行记录，需要至少一次触发。最短间隔 5 分钟；也可以直接往 `routine_fires` 写入一条已触发的记录，并在该会话 Journal 里写下对应的 `automation.routine_due`（投递身份与 `fire_id` 相同），再补上由它开始的一轮回复。

## Driving it with the browser

- **列表**：(a) `GET /api/sessions/<id>/routines` 返回一项，`state=active`、`next_run` 非空；`sqlite3 $REDPANDA_HOME/state/routines.sqlite 'select name,state,next_due from routines'` 一致。(b) 打开 `Agent 资料`，`例行任务` 一节看到 `检查 PR`、`运行中`、`每 10 分钟`、`上次 … · 下次 …`，看不到原文 `@every 10m`。(c) 其他 Agent 的会话页不出现这一项。(d) 对不存在的会话 `GET /api/sessions/nope/routines` 返回 `[]`。
- **暂停 / 恢复**：关掉开关 `暂停 检查 PR`。(a) `GET .../routines` 中 `state=paused_by_user`、`next_run=null`。(b) 状态变 `人暂停`，开关变为 `恢复 检查 PR`，下次显示 `未安排`；SSE `schedule_changed`。(c) 暂停期间到点不投递 `automation.routine_due`。(d) 接着发 `请用 set_routine_state 把「检查 PR」恢复为 active`：工具结果 `code=PAUSED_BY_USER`，状态仍是 `人暂停`。再打开 `恢复 检查 PR` → `state=active`、`next_run` 重新排上。
- **删除**：点 `删除 检查 PR`，再点 `确认删除 检查 PR`。(a) `GET .../routines` 不再含这一项，sqlite 行 `state=deleted`。(b) 列表为空时这一节显示 `无例行任务`。(c) 点 `保留` 不发请求、状态不变。(d) 对已删除的一项 `PUT /api/sessions/<id>/routines/<routine_id>/state`（错误路径探测）→ 404 并带原因；`state` 传 `paused` → 422；失效的 `connection_id` → 409。
- **详情与执行记录**：点 `查看 检查 PR`。(a) 看到意图全文；时间安排是 `每 10 分钟`，旁边仍有原文 `@every 10m`。(b) `GET /api/sessions/<id>/routines/<routine_id>/fires` 与页面顺序一致，新的在前；每条有 `planned_at`、`fired_at`，送达过的有 `delivered_at`，没送达的是 null。(c) 不存在或已删除的例行任务返回 404。(d) `返回例行任务` 回到列表。
- **跳到时间线**：对一条 `delivered_at` 非空、且对话里已有 `output_id` 与之相同的 step 的记录，点 `在时间线中查看 <触发时间>`。(a) 接口该条的 `output_id` 等于那一步的 `output_id`。(b) 时间线滚到这一轮并短暂高亮，能看到 Bot 当时的回复。(c) `delivered_at` 为 null，或回复还没出现时，没有这个按钮。
- **窄屏与时间表**：1000×800 和 390×844 打开资料抽屉，核对例行任务入口仍可操作。`*/40 * * * *`、`0 */5 * * *` 跨小时或跨日不等间隔，应保留原文；`*/15 * * * *`、`0 */6 * * *` 才能读为每 15 分钟、每 6 小时。`@every 40m` 仍读为每 40 分钟。抽屉中点执行记录跳转后，抽屉关闭且原回复可见。
- **无人查看暂停**：复用 inbox 的 absence 场景，满足离开时间、未读回复和例行触发门槛后等待询问及暂停。(a) 接口状态为 `paused_unwatched`、`next_run=null`。(b) 列表显示 `无人查看已暂停`，有 `恢复 <名字>` 和删除入口。(c) Agent 尝试恢复或删除得到 `PAUSED_UNWATCHED`，状态不变；人恢复后重新排下一次。(d) 未满足门槛时不把这一状态记为已验证。

## Gotchas

- 最短间隔 5 分钟，等一次真实触发至少要 5 分钟，放在一轮验证的最后做；只验列表、详情与按钮时可以用已写入的触发记录，不必等时钟。
- 例行任务到点开始的一轮普通回复直接给人看，见 reply-delivery。跳转落在这轮回复上，时间线里没有单独的「例行任务」气泡。
- `Agent 暂停`（`paused`）是 Agent 自己停的，人可以恢复；`人暂停` 只能由人恢复。
- 历史 live 状态：部分（2026-10-19 Windows 隔离实例 + deepseek-flash，Playwright 驱动）：Agent 用 `save_routine` 建一项后列表出现；`暂停 检查 PR` → `paused_by_user`、`next_run=null`；Agent 再调 `set_routine_state` 得到 `PAUSED_BY_USER`；`恢复 检查 PR` → `active` 并重排下一次；`取消` → `确认取消` 后接口返回 `[]`、整行消失；SSE `schedule_changed` 随每次变化到达。未等到真实触发。当时删除按钮的文案是 `取消`。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：真实 save_routine 后列表出现；人暂停后模型恢复得到 PAUSED_BY_USER，页面恢复、取消确认及列表消失通过。未完成正常例行触发；无人查看暂停未覆盖。删除按钮当时的文案是 `取消`。
- 2026-10-20 本分支（隔离 home，占位模型 ollama/dummy，`live_model_ready` 为真但没有可用模型，未发消息、未点测试）：写入两条例行任务和两次已送达触发后，侧栏列表显示可读安排、开关、上次与下次，以及 `删除`；点进 `检查 PR` 能看到意图全文、原文时间表和倒序执行记录；`在时间线中查看 11:40` 滚到对应回复并高亮。`删除` 后出现 `确认删除` 与 `保留`，点 `保留` 该项仍在。未等时钟真实触发，未覆盖无人查看暂停，未用真实模型走 `save_routine` / `PAUSED_BY_USER`。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：真实 save_routine，1100 宽度资料抽屉查看与暂停，状态变为人暂停且下次未安排；删除、失败和无人查看策略未重验。
