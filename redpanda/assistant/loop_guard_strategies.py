"""Mechanical observations only; strategies never decide whether work progresses."""

import json
from dataclasses import dataclass
from itertools import groupby

from redpanda.assistant.delivery import DELIVER_TOOL_NAME
from redpanda.runtime import DomainFactCommitted, StepCommitted, UserMessageReceived


@dataclass(frozen=True)
class Behavior:
    sequence: int
    command_id: str


@dataclass(frozen=True)
class Action(Behavior):
    tool: str
    arguments: dict

    @property
    def fingerprint(self):
        # JSON preserves arrays, strings and scalar types. 1 and 1.0 remain distinct.
        return self.tool, json.dumps(
            self.arguments, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        )


@dataclass(frozen=True)
class Evidence:
    strategy: str
    version: int
    threshold: int
    behaviors: tuple[Behavior, ...]
    observation: str


def requests_decision_from_outside(payload) -> bool:
    """A new reason to act arrived: a person, a colleague, a timer, a returned child."""
    return isinstance(payload, UserMessageReceived) or (
        isinstance(payload, DomainFactCommitted) and payload.requests_decision
    )


@dataclass(frozen=True)
class ConsecutiveActions:
    """Repeats within one wake count; the same check made once per wake does not."""

    threshold: int = 3

    def __call__(self, events):
        actions = []
        wake = 0
        for event in events:
            if requests_decision_from_outside(event.payload):
                wake += 1
                continue
            if not isinstance(event.payload, StepCommitted):
                continue
            for command in event.payload.step.commands:
                effect = command.effect
                if effect.name in {DELIVER_TOOL_NAME, "_accept_handoff"}:
                    continue
                actions.append((wake, Action(
                    event.sequence, command.command_id, effect.name, effect.argument_dict(),
                )))
        hits = []
        for _, entries in groupby(actions, key=lambda item: (item[0], item[1].fingerprint)):
            run = tuple(action for _, action in entries)
            if len(run) >= self.threshold:
                hits.append(Evidence(
                    "consecutive_action", 1, self.threshold, run,
                    f"The same tool was called {len(run)} times consecutively with identical arguments.",
                ))
        return tuple(hits)
