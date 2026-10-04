from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from redpanda.runtime.events import (
    CommandOutcomeReceived,
    DispatchAttemptStarted,
    Event,
    EventDraft,
)
from redpanda.runtime.journal.api import (
    AttemptTerminalConflict,
    Journal,
    LeaseLostError,
)
from redpanda.runtime.model import (
    AuthorizationPolicy,
    Command,
    CommandOutcome,
    CommandPhase,
    OutcomeStatus,
    RuntimeStatus,
)
from redpanda.runtime.state import StateProjector
from redpanda.runtime.step import IdFactory, random_id


@dataclass(frozen=True, slots=True)
class AttemptContext:
    session_id: str
    command_id: str
    attempt_id: str
    attempt_number: int


@dataclass(frozen=True, slots=True)
class ToolTerminal:
    outcome: CommandOutcome


@dataclass(frozen=True, slots=True)
class DispatchStartResult:
    started_command_ids: tuple[str, ...]
    status: RuntimeStatus


ToolHandler = Callable[[AttemptContext, Mapping[str, object]], Awaitable[object]]
SessionChanged = Callable[[str], Awaitable[None]]
TaskFailed = Callable[[BaseException], None]


@dataclass(frozen=True, slots=True)
class ToolBinding:
    handler: ToolHandler
    decision_on_outcome: bool = True
    requires_authorization: bool | AuthorizationPolicy = False

    def __post_init__(self) -> None:
        if type(self.decision_on_outcome) is not bool:
            raise TypeError("decision_on_outcome must be bool")
        if type(self.requires_authorization) is not bool and not callable(
            self.requires_authorization
        ):
            raise TypeError("requires_authorization must be bool or AuthorizationPolicy")


async def _stop_heartbeat(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, LeaseLostError):
        pass


class Dispatcher:
    """Execute committed Commands independently from Session Step scheduling."""

    def __init__(
        self,
        journal: Journal,
        projector: StateProjector,
        bindings: Mapping[str, ToolBinding],
        id_factory: IdFactory = random_id,
        *,
        worker_id: str = "local",
        attempt_lease_seconds: float = 30.0,
    ) -> None:
        if attempt_lease_seconds <= 0:
            raise ValueError("dispatcher lease duration must be positive")
        self._journal = journal
        self._projector = projector
        self._bindings = dict(bindings)
        self._id_factory = id_factory
        self._worker_id = worker_id
        self._attempt_lease_seconds = attempt_lease_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._session_changed: SessionChanged | None = None
        self._task_failed: TaskFailed | None = None

    def bind(self, name: str, binding: ToolBinding) -> None:
        self._bindings[name] = binding

    def connect(self, session_changed: SessionChanged, task_failed: TaskFailed) -> None:
        self._session_changed = session_changed
        self._task_failed = task_failed

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    async def start_pending(self, session_id: str) -> DispatchStartResult:
        events = await self._journal.snapshot(session_id)
        state = self._projector.project(session_id, events).state
        pending = tuple(
            item
            for item in state.commands
            if item.phase is CommandPhase.PENDING
            and item.dispatch_eligible_by_event_id is not None
            and item.authorization_rejected_by_event_id is None
            and (
                item.command.command_id not in self._tasks
                or self._tasks[item.command.command_id].done()
            )
        )
        started: list[str] = []
        for item in pending:
            command = item.command
            self._binding_for(command)
            attempt_id = self._id_factory("attempt")
            dispatch_event = await self._journal.start_attempt(
                EventDraft(
                    event_id=self._id_factory("event"),
                    session_id=session_id,
                    payload=DispatchAttemptStarted(
                        attempt_id,
                        command.command_id,
                        1,
                        self._id_factory("attempt-claim"),
                        self._worker_id,
                    ),
                    occurred_at=datetime.now(timezone.utc),
                    causation_id=item.dispatch_eligible_by_event_id,
                ),
                lease_seconds=self._attempt_lease_seconds,
            )
            if dispatch_event is None:
                continue
            task = asyncio.create_task(
                self._run(session_id, command, dispatch_event),
                name=f"agent-command:{attempt_id}",
            )
            self._tasks[command.command_id] = task
            task.add_done_callback(
                lambda done, command_id=command.command_id: self._task_done(
                    command_id, done
                )
            )
            started.append(command.command_id)
        return DispatchStartResult(tuple(started), state.status)

    def _task_done(self, command_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(command_id) is task:
            self._tasks.pop(command_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and self._task_failed is not None:
            self._task_failed(error)

    async def _run(
        self, session_id: str, command: Command, dispatch_event: Event
    ) -> None:
        payload = dispatch_event.payload
        heartbeat = asyncio.create_task(
            self._attempt_heartbeat(payload.attempt_id, payload.claim_token),
            name=f"agent-attempt-heartbeat:{payload.attempt_id}",
        )
        try:
            result = await self._binding_for(command).handler(
                AttemptContext(
                    session_id,
                    command.command_id,
                    payload.attempt_id,
                    payload.attempt_number,
                ),
                command.effect.argument_dict(),
            )
            outcome = (
                result.outcome
                if isinstance(result, ToolTerminal)
                else CommandOutcome(OutcomeStatus.SUCCEEDED, value=result)
            )
            await self.accept_outcome(
                session_id,
                command.command_id,
                payload.attempt_id,
                dispatch_event.event_id,
                outcome,
            )
        finally:
            await _stop_heartbeat(heartbeat)
            await self._journal.release_attempt(payload.attempt_id, payload.claim_token)
        if self._session_changed is not None:
            await self._session_changed(session_id)

    async def _attempt_heartbeat(self, attempt_id: str, claim_token: str) -> None:
        while True:
            await asyncio.sleep(self._attempt_lease_seconds / 3)
            if not await self._journal.renew_attempt(
                attempt_id, claim_token, lease_seconds=self._attempt_lease_seconds
            ):
                raise LeaseLostError(claim_token)

    async def accept_outcome(
        self,
        session_id: str,
        command_id: str,
        attempt_id: str,
        causation_event_id: str,
        outcome: CommandOutcome,
    ) -> Event | None:
        """接纳 Attempt 终局。同一 Attempt 只保留先写入的那条。"""

        try:
            return await self._journal.record_attempt_fact(
                EventDraft(
                    event_id=self._id_factory("event"),
                    session_id=session_id,
                    payload=CommandOutcomeReceived(command_id, attempt_id, outcome),
                    occurred_at=datetime.now(timezone.utc),
                    causation_id=causation_event_id,
                )
            )
        except AttemptTerminalConflict:
            events = await self._journal.snapshot(session_id)
            for event in reversed(events):
                payload = event.payload
                if (
                    isinstance(payload, CommandOutcomeReceived)
                    and payload.attempt_id == attempt_id
                ):
                    return event
            raise

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _binding_for(self, command: Command) -> ToolBinding:
        return self._bindings[command.effect.name]
