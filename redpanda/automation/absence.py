"""Ask, then pause, when nobody is watching an Agent's routines.

Each routine fire is a new origin, so the message budget does not stop it.
The Host counts unread reports and fires while the person is away from Web.
Past both thresholds it asks in the inbox; if that question sits unanswered
for the grace period, it pauses the routines that are still running.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from redpanda.assistant.agent_messages import MESSAGE_USER
from redpanda.automation.routines import ROUTINE_DUE, AbsenceWatch
from redpanda.runtime import CommandOutcomeReceived, DomainFactCommitted, replay


# Starting values, tuned the same way as hop and origin limits.
AWAY_BEFORE_ASK = timedelta(hours=12)
ASK_GRACE = timedelta(hours=12)
UNREAD_REPORTS = 3
ROUTINE_TRIGGERS = 3
ABSENCE_REVIEW_SECONDS = 60

PAUSED_TEXT = "你离开后没有人查看，这些仍在运行的例行任务已暂停。人恢复后它们才会继续。不要自行恢复。"
RESUMED_TEXT = "人选择让这些例行任务继续。被你自己或人另行暂停的不在其中。"


def away_long_enough(last_seen: datetime | None, now: datetime) -> bool:
    return last_seen is None or now - last_seen >= AWAY_BEFORE_ASK


def next_absence_action(
    *,
    present: bool,
    now: datetime,
    last_seen: datetime | None,
    watch: AbsenceWatch | None,
    reports: int,
    triggers: int,
) -> str | None:
    """``ask``, ``pause``, or ``record_fact``. ``None`` leaves the Agent alone."""
    if watch is not None and watch.phase == "paused":
        return None if watch.fact_recorded else "record_fact"
    if present or not away_long_enough(last_seen, now):
        return None
    if watch is not None and watch.phase == "asked":
        return "pause" if now - watch.asked_at >= ASK_GRACE else None
    if reports >= UNREAD_REPORTS and triggers >= ROUTINE_TRIGGERS:
        return "ask"
    return None


def counts_since(session_id: str, events, since: datetime | None) -> tuple[int, int]:
    """Unread ``message_user`` results and routine fires strictly after ``since``."""
    sent = {
        command.command.command_id
        for command in replay(session_id, events).state.commands
        if command.command.effect.name == MESSAGE_USER
    }
    reports = triggers = 0
    for event in events:
        if since is not None and event.occurred_at <= since:
            continue
        payload = event.payload
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == ROUTINE_DUE:
            triggers += 1
        elif isinstance(payload, CommandOutcomeReceived) and payload.command_id in sent:
            value = payload.outcome.value
            if isinstance(value, Mapping) and value.get("code") == "MESSAGE_SENT":
                reports += 1
    return reports, triggers
