from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from redpanda.runtime.events import Event, EventDraft, StepCommitted
from redpanda.runtime.journal.api import Journal, StepLease
from redpanda.runtime.model import (
    AuthorizationPolicy,
    Command,
    ModelDecision,
    Step,
)
from redpanda.runtime.state import DecisionFrame, StateProjector


IdFactory = Callable[[str], str]


def random_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class DecisionMaker(Protocol):
    async def decide(
        self,
        frame: DecisionFrame,
    ) -> ModelDecision | RecordedDecision: ...


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    """Adapter 提供的决策、不透明证据引用及原子提交元数据。"""

    decision: ModelDecision
    artifact_refs: tuple[str, ...] = ()
    decision_metadata: object = None

    def __post_init__(self) -> None:
        if type(self.decision) is not ModelDecision:
            raise TypeError("recorded decision is invalid")
        if type(self.artifact_refs) is not tuple or any(
            type(item) is not str or not item for item in self.artifact_refs
        ):
            raise ValueError("decision artifact refs are invalid")
        if len(self.artifact_refs) != len(set(self.artifact_refs)):
            raise ValueError("decision artifact refs contain duplicates")


class StepRunner:
    def __init__(
        self,
        journal: Journal,
        projector: StateProjector,
        decision_maker: DecisionMaker,
        id_factory: IdFactory = random_id,
        requires_authorization: Mapping[str, bool | AuthorizationPolicy] | None = None,
        decision_on_outcome: Mapping[str, bool] | None = None,
    ) -> None:
        self._journal = journal
        self._projector = projector
        self._decision_maker = decision_maker
        self._requires_authorization = dict(
            {} if requires_authorization is None else requires_authorization
        )
        self._decision_on_outcome = dict(
            {} if decision_on_outcome is None else decision_on_outcome
        )
        self._id_factory = id_factory

    def bind(
        self,
        name: str,
        *,
        decision_on_outcome: bool,
        requires_authorization: bool | AuthorizationPolicy = False,
    ) -> None:
        self._decision_on_outcome[name] = decision_on_outcome
        self._requires_authorization[name] = requires_authorization

    async def commit(
        self,
        frame: DecisionFrame,
        lease: StepLease,
    ) -> Event:
        result = await self._decision_maker.decide(frame)
        if type(result) is RecordedDecision:
            decision = result.decision
            artifact_refs = result.artifact_refs
            decision_metadata = result.decision_metadata
        else:
            decision = result
            artifact_refs = ()
            decision_metadata = None
        commands: list[Command] = []
        for request in decision.command_requests:
            command_id = self._id_factory("command")
            policy = self._requires_authorization[request.name]
            command = Command(
                command_id=command_id,
                effect=request,
                requires_authorization=(
                    policy(request.argument_dict()) if callable(policy) else policy
                ),
                decision_on_outcome=(self._decision_on_outcome[request.name]),
            )
            commands.append(command)
        command_tuple = tuple(commands)
        step = Step(
            step_id=self._id_factory("step"),
            trigger_event_id=frame.trigger_event.event_id,
            decision_cursor=frame.decision_cursor,
            basis_state_version=frame.basis_state_version,
            observed_journal_position=frame.observed_journal_position,
            decision=decision,
            commands=command_tuple,
        )
        return await self._journal.commit_step(
            lease,
            EventDraft(
                event_id=self._id_factory("event"),
                session_id=frame.trigger_event.session_id,
                payload=StepCommitted(step, decision_metadata),
                occurred_at=datetime.now(timezone.utc),
                causation_id=frame.trigger_event.event_id,
                artifact_refs=artifact_refs,
            ),
        )
