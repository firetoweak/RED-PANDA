from __future__ import annotations

from datetime import datetime, timezone

from redpanda.automation.tool import (
    CANCEL_SCHEDULE,
    SCHEDULE_ONCE,
    SessionTransport,
    cancel_schedule,
    register_schedule,
)
from redpanda.runtime import (
    CommandOutcome,
    CommandOutcomeReceived,
    EventDraft,
    OutcomeStatus,
    SqliteJournal,
    replay,
)
from redpanda.runtime.model import CommandPhase


async def recover_schedule_attempts(
    journal: SqliteJournal,
    session_id: str,
    transport: SessionTransport,
) -> None:
    """Resolve only schedule attempts whose durable effect can be checked by id."""

    events = await journal.snapshot(session_id)
    state = replay(session_id, events).state
    by_id = {event.event_id: event for event in events}
    for item in state.commands:
        if (
            item.command.effect.name not in (SCHEDULE_ONCE, CANCEL_SCHEDULE)
            or item.phase is not CommandPhase.UNKNOWN
        ):
            continue
        (attempt,) = item.attempts
        started = by_id[attempt.started_event_id]
        if item.command.effect.name == CANCEL_SCHEDULE:
            result = await cancel_schedule(
                transport,
                session_id,
                item.command.command_id,
                item.command.effect.argument_dict(),
            )
        else:
            result = await register_schedule(
                transport,
                session_id,
                item.command.command_id,
                item.command.effect.argument_dict(),
                started_at=started.occurred_at.isoformat(),
            )
        await journal.record_attempt_fact(
            EventDraft(
                event_id=f"schedule-recovered:{item.command.command_id}",
                session_id=session_id,
                payload=CommandOutcomeReceived(
                    item.command.command_id,
                    attempt.attempt_id,
                    CommandOutcome(OutcomeStatus.SUCCEEDED, value=result),
                ),
                occurred_at=datetime.now(timezone.utc),
                causation_id=attempt.started_event_id,
            )
        )
