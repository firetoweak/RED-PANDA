# 定时唤醒

助手可用 `schedule_once` 安排一次性稍后提醒，用 `cancel_schedule` 取消；等待期间输入框上方显示倒计时一行。来源：`redpanda/automation/tool.py`（工具参数与结果）、`redpanda/automation/once.py`（`OneShotSchedules` 存储、`OneShotClock.run` 触发循环、事实 `automation.time_reached`）、`redpanda/automation/recovery.py`（只补全重启前 UNKNOWN 的 `schedule_once`/`cancel_schedule` 调用）、`redpanda/assistant/host/supervisor.py`（`_deliver_scheduled`、`schedule_changed`）、`redpanda/assistant/conversations.py`（`waiting_until`）、`ScheduledWait.tsx`。存储 `$REDPANDA_HOME/state/automation.sqlite`。

## Sub-features

- `schedule`：`schedule_once`，参数 `delay_seconds`（整数 1..31536000）与 `purpose`（必填非空字符串），只接受这两个参数；成功结果 `code=SCHEDULED`，`data` 含 `schedule_id`、`due_at`、`purpose`。
- `wait-indicator`：会话 `waiting_until` 非空时，输入框上方一行时钟图标 + 等宽英文 `Waiting up to MMm SSs`，到点后 `Scheduled time reached…`；不是时间线卡片。
- `fire`：到点后向会话投递事实 `automation.time_reached`（含 `schedule_id`、`purpose`、计划时间），开启新一轮。
- `cancel`：`cancel_schedule`，结果码 `CANCELLED`/`ALREADY_FIRED`/`NOT_FOUND`（只有 `CANCELLED` 时 `ok=true`）。
- `persist-across-restart`：未投递的定时存在 sqlite 里，重启后触发循环从 `next_pending` 接着等。

## How to get to it (user POV)

- 发 `请用 schedule_once 安排 60 秒后继续，然后回复 SCHEDULED；到时间后回复 WOKE_UP。`

## Driving it with the browser

- **安排与触发**：
  - (a) `sqlite3 $REDPANDA_HOME/state/automation.sqlite 'select schedule_id,due_at,fired_at,delivered_at from schedules'` 出现一行，`fired_at`、`delivered_at` 为空；`GET /api/sessions/<id>` 的 `waiting_until` 等于该行 `due_at`。到点后该行**不删除**：先写 `fired_at`，投递后写 `delivered_at`；`waiting_until` 变回 null。
  - (b) 工具结果 `code=SCHEDULED`；SSE 收到 `schedule_changed`（安排时与投递后各一次）；输入框上方 `Waiting up to 00m 5xs` 倒计时；约 60 秒后会话多一轮，回复含 `WOKE_UP`，该轮的触发是 `automation.time_reached`。
  - (c) 无用户输入也会触发，但只触发一次（`delivered_at` 写入后不再投递）；侧栏不永久停在运行状态。
  - (d) 让模型调用 `schedule_once` 不带 `purpose`（或 `delay_seconds=0`）→ `ok=false`、`code=INVALID_ARGUMENT`，`automation.sqlite` 不新增行。
- **取消**：安排后发 `请取消刚才的定时`。(a) `schedule_cancellations` 表出现一行 `code=CANCELLED`；`schedules` 行仍在、`fired_at` 为空；`waiting_until` 变回 null。(b) 工具结果 `code=CANCELLED`、`ok=true`；倒计时消失；SSE `schedule_changed`。(c) 到点后没有新一轮。(d) 对已触发的定时取消 → `ALREADY_FIRED`；对不存在的 id 取消 → `NOT_FOUND`，两者 `ok=false`。
- **重启后仍触发**：安排 120 秒，30 秒后杀掉服务（命令见 crash-recovery）并用同一 `REDPANDA_HOME` 重启。(a) 重启后 `schedules` 行仍在、`delivered_at` 为空，`waiting_until` 不变；到点后写入 `fired_at` 与 `delivered_at`。(b) 页面重新显示倒计时；到点后会话多一轮。(c) 只触发一次。(d) 重启后丢失或触发两次 → 记为问题。

## Gotchas

- 时间较长，放在一轮验证的最后做。
- 倒计时文字是英文（`Waiting up to ...`），照原样匹配，不要找中文。
- 重启后的持久靠 sqlite + `OneShotClock.run` 的 `next_pending`，不是 `recovery.py`；`recovery.py` 只处理重启前「已开始但没结果」的 `schedule_once`/`cancel_schedule` 调用。
- 重启后仍可运行的长期 Agent 自动续跑；人停止过的、或遗留结果未知且中断尚未被后续决策看见的 `execute_command` 保持 paused（见 crash-recovery）。定时事实仍会投递，暂停的会话需要点 `继续` 或发新消息才推进。
- 历史 live 状态：通过（2026-10-12，main@1a4790a），重启后持久性未测。2026-10-18 按源码改写，未重新 live 验证。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：UI 请求 45 秒 schedule_once，倒计时可见，无新用户输入即回复 WOKE_UP；重启持久性、取消与非法参数未重验。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：真实 schedule_once 45 秒倒计时可见，无新用户输入即 MAP_WAKE；取消、重启持久化与错参未重验。
