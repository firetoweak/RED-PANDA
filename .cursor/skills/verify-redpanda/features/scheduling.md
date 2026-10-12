# 定时唤醒

助手可用 `schedule_once` 安排一次性稍后继续，用 `cancel_schedule` 取消；时间线显示等待状态。来源：`redpanda/automation/tool.py`、`once.py`、`recovery.py`、`ScheduledWait.tsx`，存储 `$REDPANDA_HOME/state/automation.sqlite`。

## Sub-features

- `schedule`：安排 N 秒后继续，会话显示等待卡片。
- `fire`：到点后自动开启新一轮。
- `cancel`：`cancel_schedule` 取消。
- `recovery`：服务重启后未到期的定时仍然生效（`recovery.py`）。

## How to get to it (user POV)

- 发 `请用 schedule_once 安排 60 秒后继续，然后回复 SCHEDULED；到时间后回复 WOKE_UP。`

## Driving it with the browser

- **安排与触发**：(a) `automation.sqlite` 中出现一条记录（`sqlite3 ... .tables` 后查询）；约 60 秒后会话多出一轮含 `WOKE_UP`，记录变为已完成或被删除。(b) 等待期间显示 `ScheduledWait` 卡片，侧栏状态合理（非永久 `运行中`）。(c) 无用户输入也会触发，但不得重复触发两次。
- **取消**：安排后发 `请取消刚才的定时`。(a) 到点后没有新一轮。
- **重启恢复**：安排 120 秒，30 秒后杀掉并用同一 `REDPANDA_HOME` 重启服务。(a) 到点后仍触发一次。(d) 若重启后丢失或触发两次 → 记为问题。

## Gotchas

- 时间较长，放在一轮验证的最后做。
