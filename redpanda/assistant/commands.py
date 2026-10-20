"""Background command facts and narrow recovery of interrupted handoffs."""
from datetime import datetime, timezone

from redpanda.runtime import (
    CommandOutcome, CommandOutcomeReceived, CommandPhase, DomainFactCommitted,
    EventDraft, OutcomeStatus, StepCommitted, replay,
)
from redpanda.runtime.json_values import thaw_value
from redpanda.assistant.builtin_tools import command_result
from redpanda.execution.processes import command_identity


COMMAND_FINISHED = "background_command_finished"
SOURCE = "command"


def background_commands(events):
    commands = {command.command_id: command for event in events
                if isinstance(event.payload, StepCommitted) for command in event.payload.step.commands}
    for event in events:
        if isinstance(event.payload, CommandOutcomeReceived):
            value = thaw_value(event.payload.outcome.value)
            if isinstance(value, dict) and value.get("code") == "COMMAND_BACKGROUND":
                identity = event.payload.command_id
                yield identity, {**value["data"]["background"],
                                 "command": commands[identity].effect.argument_dict()["command"]}


def completed_commands(events):
    return {event.delivery.delivery_id for event in events
            if isinstance(event.payload, DomainFactCommitted)
            and event.delivery is not None and event.delivery.source == SOURCE
            and event.payload.fact_type == COMMAND_FINISHED}


async def recover_command_attempts(journal, session_id, transport):
    # Import here keeps the Session-bound IPC adapter outside projection imports.
    from redpanda.assistant.host.command_port import execution_result

    events = await journal.snapshot(session_id)
    by_id = {event.event_id: event for event in events}
    for item in replay(session_id, events).state.commands:
        if item.command.effect.name != "execute_command" or item.phase is not CommandPhase.UNKNOWN:
            continue
        (attempt,) = item.attempts
        response = await transport("command_query", session_id, {
            "command_id": command_identity(session_id, item.command.command_id),
            "initiating_id": item.command.command_id,
            "started_at": by_id[attempt.started_event_id].occurred_at.isoformat(),
        })
        if response["status"] == "unknown":
            continue
        if response["status"] == "not_started":
            result = {"ok": False, "code": "COMMAND_NOT_STARTED", "error": "命令尚未启动，未执行。"}
        else:
            data = response["command"]
            result = command_result(execution_result(data, interrupted=True), data["cwd"])
        await journal.record_attempt_fact(EventDraft(
            event_id=f"command-recovered:{item.command.command_id}", session_id=session_id,
            payload=CommandOutcomeReceived(item.command.command_id, attempt.attempt_id,
                                          CommandOutcome(OutcomeStatus.SUCCEEDED, value=result)),
            occurred_at=datetime.now(timezone.utc), causation_id=attempt.started_event_id,
        ))
