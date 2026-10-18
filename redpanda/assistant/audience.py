"""Who a long-lived Agent's closing text is for, decided from the facts alone.

The closing text of a turn goes to the person only when the turn traces back to
something the person said. Command results, a delegate's report and a one-shot
reminder continue the turn that issued them; a colleague's message, the group
calling on the Agent and a routine coming due start a turn of their own, whose
closing text stays in the Agent's work log.
"""

from __future__ import annotations

from typing import Literal

from redpanda.assistant.agents import GROUP_ATTENTION, PRIVATE_MESSAGE, project_agent
from redpanda.assistant.subagent.subagent import DELEGATE, REPORT_FACT, child_session_id
from redpanda.automation.once import TIME_REACHED
from redpanda.automation.routines import ROUTINE_DUE
from redpanda.runtime import (
    AttemptOrphaned, CommandOutcomeReceived, CommandRejected, DomainFactCommitted, Event, replay,
)

Audience = Literal["user", "log"]
OWN_TURNS = frozenset({PRIVATE_MESSAGE, GROUP_ATTENTION, ROUTINE_DUE})


def audiences(events: tuple[Event, ...]):
    """Return a lookup from a Step's trigger event to the audience of its closing text."""
    if not events or project_agent(events) is None:
        return lambda _trigger_event_id: "user"
    session_id = events[0].session_id
    state = replay(session_id, events).state
    issued = {item.command.command_id: item.issued_by_event_id for item in state.commands}
    triggers = {item.committed_event_id: item.step.trigger_event_id for item in state.steps}
    delegates = {child_session_id(session_id, item.command.command_id): item.command.command_id
                 for item in state.commands if item.command.effect.name == DELEGATE}
    by_id = {event.event_id: event for event in events}

    def issuing_command(payload) -> str | None:
        if isinstance(payload, (CommandOutcomeReceived, CommandRejected, AttemptOrphaned)):
            return payload.command_id
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == TIME_REACHED:
            return payload.data["schedule_id"]
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == REPORT_FACT:
            return delegates[payload.data["child_session_id"]]
        return None

    def audience(trigger_event_id: str) -> Audience:
        trigger = by_id[trigger_event_id]
        while (command_id := issuing_command(trigger.payload)) is not None:
            trigger = by_id[triggers[issued[command_id]]]
        payload = trigger.payload
        if isinstance(payload, DomainFactCommitted) and payload.fact_type in OWN_TURNS:
            return "log"
        return "user"

    return audience
