# 例行任务

长期 Agent 用 `save_routine` / `set_routine_state` / `list_routines` 管理自己的例行任务；人在该 Agent 的会话页输入框上方的 `例行任务` 一行里查看、暂停、恢复与取消。来源：`redpanda/automation/routines.py`（`RoutineStore`、`RoutineClock`、状态 `active`/`paused`/`paused_by_user`/`paused_unwatched`/`deleted`、事实 `automation.routine_due`）、`redpanda/automation/routine_tool.py`、`redpanda/assistant/host/supervisor.py`（`list_routines`、`set_routine_state_by_user`、`schedule_changed`）、`redpanda/channels/web/channel.py`、`web/src/features/conversation/RoutineList.tsx`。存储 `$REDPANDA_HOME/state/routines.sqlite`。

## Sub-features

- `list`：输入框上方一行 `例行任务`（按钮，`aria-expanded`），右侧 `N 项`，有暂停的加 `· M 项暂停`；点开后每项显示名字、状态徽标（`运行中` / `Agent 暂停` / `人暂停` / `无人查看已暂停`）、时间表与 `下一次 <时间>`（暂停时没有下一次）。没有例行任务时整行不出现。
- `pause`：运行中的一项有按钮 `暂停 <名字>`，点后状态变 `人暂停`。
- `resume`：暂停态（`Agent 暂停`、`人暂停` 或 `无人查看已暂停`）的一项有按钮 `恢复 <名字>`，点后回到 `运行中` 并重新排下一次。
- `cancel`：每项有 `取消 <名字>`；点后出现 `确认取消 <名字>` 与 `保留`，确认后这一项从列表消失。
- `user-pause-sticks`：人暂停的一项，Agent 调 `set_routine_state` 恢复或删除会得到 `code=PAUSED_BY_USER`，只能由人恢复或删除。
- `unwatched-pause`：无人查看询问及暂停见 [inbox.md](./inbox.md)；`paused_unwatched` 显示 `无人查看已暂停`，Agent 调 `set_routine_state` 恢复或删除会得到 `PAUSED_UNWATCHED`，只能由人处理。
- `refresh`：Agent 自己新建、改写或暂停例行任务、到点触发后，SSE `schedule_changed` 让列表重读，不需要刷新页面。

## How to get to it (user POV)

- 打开一个 Agent 的会话，发 `请用 save_routine 建一个例行任务，名字「检查 PR」，每 10 分钟一次（@every 10m），intent 写「看看有没有新评审」，然后回复 SAVED。`
- 回复后输入框上方出现 `例行任务 1 项`。

## Driving it with the browser

- **列表**：(a) `GET /api/sessions/<id>/routines` 返回一项，`state=active`、`next_run` 非空；`sqlite3 $REDPANDA_HOME/state/routines.sqlite 'select name,state,next_due from routines'` 一致。(b) 点开 `例行任务` 看到 `检查 PR`、`运行中`、`@every 10m · 下一次 …`。(c) 其他 Agent 的会话页不出现这一项。(d) 对不存在的会话 `GET /api/sessions/nope/routines` 返回 `[]`，页面不占位。
- **暂停 / 恢复**：点 `暂停 检查 PR`。(a) `GET .../routines` 中 `state=paused_by_user`、`next_run=null`。(b) 徽标变 `人暂停`，按钮变 `恢复 检查 PR`；SSE `schedule_changed`。(c) 暂停期间到点不投递 `automation.routine_due`。(d) 接着发 `请用 set_routine_state 把「检查 PR」恢复为 active`：工具结果 `code=PAUSED_BY_USER`，状态仍是 `人暂停`。再点 `恢复 检查 PR` → `state=active`、`next_run` 重新排上。
- **取消**：点 `取消 检查 PR`，再点 `确认取消 检查 PR`。(a) `GET .../routines` 不再含这一项，sqlite 行 `state=deleted`。(b) 列表为空时整行 `例行任务` 消失。(c) 点 `保留` 不发请求、状态不变。(d) 对已删除的一项 `PUT /api/sessions/<id>/routines/<routine_id>/state`（错误路径探测）→ 404 并带原因；`state` 传 `paused` → 422；失效的 `connection_id` → 409。
- **无人查看暂停**：复用 inbox 的 absence 场景，满足离开时间、未读回复和例行触发门槛后等待询问及暂停。(a) 接口状态为 `paused_unwatched`、`next_run=null`。(b) 列表显示 `无人查看已暂停`，有 `恢复 <名字>` 和取消入口。(c) Agent 尝试恢复或删除得到 `PAUSED_UNWATCHED`，状态不变；人恢复后重新排下一次。(d) 未满足门槛时不把这一状态记为已验证。

## Gotchas

- 最短间隔 5 分钟，等一次真实触发至少要 5 分钟，放在一轮验证的最后做；只验列表与按钮时不必等触发。
- 例行任务到点开始的一轮普通回复直接给人看，见 reply-delivery。
- `Agent 暂停`（`paused`）是 Agent 自己停的，人可以恢复；`人暂停` 只能由人恢复。
- 历史 live 状态：部分（2026-10-19 Windows 隔离实例 + deepseek-flash，Playwright 驱动）：Agent 用 `save_routine` 建一项后列表出现；`暂停 检查 PR` → `paused_by_user`、`next_run=null`；Agent 再调 `set_routine_state` 得到 `PAUSED_BY_USER`；`恢复 检查 PR` → `active` 并重排下一次；`取消` → `确认取消` 后接口返回 `[]`、整行消失；SSE `schedule_changed` 随每次变化到达。未等到真实触发。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：真实 save_routine 后列表出现；人暂停后模型恢复得到 PAUSED_BY_USER，页面恢复、取消确认及列表消失通过。未完成正常例行触发；无人查看暂停未覆盖。
