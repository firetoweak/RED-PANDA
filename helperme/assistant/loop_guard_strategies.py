"""Mechanical observations only; strategies never decide whether work progresses."""

import json
from dataclasses import dataclass
from itertools import groupby

from helperme.assistant.delivery import DELIVER_TOOL_NAME
from helperme.runtime import StepCommitted


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


@dataclass(frozen=True)
class ConsecutiveActions:
    threshold: int = 3

    def __call__(self, events):
        actions = []
        for event in events:
            if not isinstance(event.payload, StepCommitted):
                continue
            for command in event.payload.step.commands:
                effect = command.effect
                if effect.name in {DELIVER_TOOL_NAME, "_accept_handoff"}:
                    continue
                actions.append(Action(
                    event.sequence, command.command_id, effect.name, effect.argument_dict(),
                ))
        hits = []
        for _, group in groupby(actions, key=lambda action: action.fingerprint):
            run = tuple(group)
            if len(run) >= self.threshold:
                hits.append(Evidence(
                    "consecutive_action", 1, self.threshold, run,
                    f"连续 {len(run)} 次用相同参数调用了相同工具。",
                ))
        return tuple(hits)
