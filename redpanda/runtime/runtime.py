from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256

from redpanda.runtime.dispatcher import Dispatcher, ToolBinding
from redpanda.runtime.events import (
    CommandAuthorized,
    CommandRejected,
    DecisionCancelled,
    DomainFactCommitted,
    Event,
    EventDraft,
    DeliveryIdentity,
    StepContinuationCancelled,
    UserMessageReceived,
)
from redpanda.runtime.journal.api import Journal, LeaseLostError, StepClaimRequest
from redpanda.runtime.model import CanonicalState, CommandPhase, RuntimeStatus, Step
from redpanda.runtime.projections import (
    ReplayView,
    TraceView,
    project_trace,
    replay,
)
from redpanda.runtime.state import StateProjector
from redpanda.runtime.step import DecisionMaker, IdFactory, StepRunner, random_id


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    step: Step | None
    status: RuntimeStatus


@dataclass(slots=True)
class _ActiveDecision:
    trigger_event_id: str
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    cancellation: Event | None = None


async def _cancel_task(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _stop_heartbeat(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, LeaseLostError):
        pass


class AgentRuntime:
    def __init__(
        self,
        journal: Journal,
        decision_maker: DecisionMaker,
        tool_bindings: Mapping[str, ToolBinding],
        id_factory: IdFactory = random_id,
        *,
        worker_id: str | None = None,
        step_lease_seconds: float = 30.0,
        attempt_lease_seconds: float = 30.0,
    ) -> None:
        if step_lease_seconds <= 0:
            raise ValueError("step lease duration must be positive")
        self._journal = journal
        self._id_factory = id_factory
        self._worker_id = id_factory("worker") if worker_id is None else worker_id
        self._step_lease_seconds = step_lease_seconds
        self.projector = StateProjector()
        self.step_runner = StepRunner(
            journal,
            self.projector,
            decision_maker,
            id_factory,
            requires_authorization={
                name: binding.requires_authorization
                for name, binding in tool_bindings.items()
            },
            decision_on_outcome={
                name: binding.decision_on_outcome
                for name, binding in tool_bindings.items()
            },
        )
        self.dispatcher = Dispatcher(
            journal,
            self.projector,
            tool_bindings,
            id_factory,
            worker_id=self._worker_id,
            attempt_lease_seconds=attempt_lease_seconds,
        )
        self._step_locks: dict[str, asyncio.Lock] = {}
        self._active_decisions: dict[str, _ActiveDecision] = {}

    def bind_tool(
        self,
        name: str,
        binding: ToolBinding,
    ) -> None:
        self.dispatcher.bind(name, binding)
        self.step_runner.bind(
            name,
            decision_on_outcome=binding.decision_on_outcome,
            requires_authorization=binding.requires_authorization,
        )

    async def create_session(self, session_id: str) -> bool:
        """Persist a Host-selected Session identity without creating an Event."""

        return await self._journal.create_session(session_id)

    async def session_exists(self, session_id: str) -> bool:
        return await self._journal.session_exists(session_id)

    async def _append_external(
        self,
        session_id: str,
        payload: UserMessageReceived | DomainFactCommitted,
        delivery: DeliveryIdentity,
        *,
        causation_id: str | None = None,
        artifact_refs: tuple[str, ...] = (),
    ) -> Event:
        result = await self._journal.accept_delivery(
            EventDraft(
                event_id=self._id_factory("event"),
                session_id=session_id,
                payload=payload,
                occurred_at=datetime.now(timezone.utc),
                causation_id=causation_id,
                artifact_refs=artifact_refs,
                delivery=delivery,
            )
        )
        return result.event

    async def receive_domain_fact(
        self,
        session_id: str,
        fact_type: str,
        data: object,
        *,
        delivery_id: str,
        source: str,
        requests_decision: bool = False,
        causation_id: str | None = None,
    ) -> Event:
        """Accept a non-human external fact for an already selected Session."""

        return await self._append_external(
            session_id,
            DomainFactCommitted(
                fact_type,
                data,
                requests_decision=requests_decision,
            ),
            DeliveryIdentity(source, delivery_id),
            causation_id=causation_id,
        )

    async def snapshot(self, session_id: str) -> tuple[Event, ...]:
        return await self._journal.snapshot(session_id)

    async def receive_user_message(
        self,
        session_id: str,
        content: str,
        *,
        delivery_id: str,
        source: str = "user",
        artifact_refs: tuple[str, ...] = (),
    ) -> Event:
        return await self._append_external(
            session_id,
            UserMessageReceived(content),
            DeliveryIdentity(source, delivery_id),
            artifact_refs=tuple(artifact_refs),
        )

    async def cancel_turn(self, session_id: str) -> tuple[Event, ...]:
        """关闭当前自动决策链。

        必须早于 Assistant 终止进程：否则中断终局会在续步权还开着时落库，
        模型不知道本轮已被取消就多走一步。
        """

        recorded: list[Event] = []
        while True:
            active = self._active_decisions.get(session_id)
            if active is not None:
                cancellation = await self._journal.cancel_decision(
                    EventDraft(
                        event_id=self._id_factory("event"),
                        session_id=session_id,
                        payload=DecisionCancelled(active.trigger_event_id),
                        occurred_at=datetime.now(timezone.utc),
                        causation_id=active.trigger_event_id,
                    )
                )
                active.cancellation = cancellation
                active.cancel_requested.set()
                await active.finished.wait()
                if cancellation is not None:
                    return (*recorded, cancellation)
                continue

            events = await self._journal.snapshot(session_id)
            projection = self.projector.project(session_id, events)
            frame = projection.next_decision
            if frame is not None:
                cancellation = await self._journal.cancel_decision(
                    EventDraft(
                        event_id=self._id_factory("event"),
                        session_id=session_id,
                        payload=DecisionCancelled(frame.trigger_event.event_id),
                        occurred_at=datetime.now(timezone.utc),
                        causation_id=frame.trigger_event.event_id,
                    )
                )
                if cancellation is not None:
                    return (*recorded, cancellation)
                continue

            cancelled_step_ids = {
                event.payload.step_event_id
                for event in events
                if isinstance(event.payload, StepContinuationCancelled)
            }
            step_event_ids = {
                command.issued_by_event_id
                for command in projection.state.commands
                if command.phase is not CommandPhase.TERMINAL
                and command.authorization_rejected_by_event_id is None
                and command.command.decision_on_outcome
                and command.issued_by_event_id not in cancelled_step_ids
            }
            cancellations: list[Event] = []
            for step_event_id in step_event_ids:
                cancellation = await self._journal.cancel_continuation(
                    EventDraft(
                        event_id=self._id_factory("event"),
                        session_id=session_id,
                        payload=StepContinuationCancelled(step_event_id),
                        occurred_at=datetime.now(timezone.utc),
                        causation_id=step_event_id,
                    )
                )
                if cancellation is not None:
                    cancellations.append(cancellation)
            if cancellations:
                recorded.extend(cancellations)
                continue
            if not step_event_ids:
                return tuple(recorded)

    async def grant_command(
        self,
        session_id: str,
        command_id: str,
    ) -> Event | None:
        event = await self._journal.grant_command(
            EventDraft(
                event_id=self._id_factory("event"),
                session_id=session_id,
                payload=CommandAuthorized(command_id),
                occurred_at=datetime.now(timezone.utc),
            )
        )
        if event is not None:
            await self.dispatcher.start_pending(session_id)
        return event

    async def reject_command(
        self,
        session_id: str,
        command_id: str,
    ) -> Event | None:
        return await self._journal.reject_command(
            EventDraft(
                event_id=self._id_factory("event"),
                session_id=session_id,
                payload=CommandRejected(command_id),
                occurred_at=datetime.now(timezone.utc),
            )
        )

    async def advance(
        self, session_id: str, *, expected_journal_position: int | None = None
    ) -> AdvanceResult:
        lock = self._step_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            events = await self._journal.snapshot(session_id)
            position = events[-1].sequence if events else 0
            if expected_journal_position is not None and position != expected_journal_position:
                return AdvanceResult(None, RuntimeStatus.RUNNABLE)
            frame = self.projector.project(
                session_id,
                events,
            ).next_decision
            if frame is None:
                dispatch = await self.dispatcher.start_pending(session_id)
                return AdvanceResult(None, dispatch.status)
            request = StepClaimRequest(
                session_id=session_id,
                trigger_event_id=frame.trigger_event.event_id,
                decision_cursor=frame.decision_cursor,
                basis_state_version=frame.basis_state_version,
                observed_journal_position=frame.observed_journal_position,
            )
            lease = await self._journal.acquire_step(
                request,
                token=self._id_factory("step-claim"),
                owner_id=self._worker_id,
                lease_seconds=self._step_lease_seconds,
            )
            if lease is None:
                return AdvanceResult(None, RuntimeStatus.RUNNABLE)
            active = _ActiveDecision(frame.trigger_event.event_id)
            if session_id in self._active_decisions:
                raise RuntimeError(f"decision already active: {session_id}")
            self._active_decisions[session_id] = active
            heartbeat = asyncio.create_task(
                self._step_heartbeat(lease),
                name=f"agent-step-heartbeat:{lease.token}",
            )
            operation = asyncio.create_task(
                self.step_runner.commit(frame, lease),
                name=f"agent-step:{lease.token}",
            )
            cancellation = asyncio.create_task(
                active.cancel_requested.wait(),
                name=f"agent-step-cancel:{lease.token}",
            )
            try:
                done, _ = await asyncio.wait(
                    (operation, heartbeat, cancellation),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if operation in done:
                    step_event = await operation
                elif cancellation in done:
                    if not operation.done():
                        operation.cancel()
                    try:
                        step_event = await operation
                    except asyncio.CancelledError:
                        return AdvanceResult(
                            None,
                            (await self.state(session_id)).status,
                        )
                else:
                    error = heartbeat.exception()
                    if error is None:
                        raise RuntimeError("step lease heartbeat stopped")
                    raise error
            except LeaseLostError:
                await self._journal.release_step(lease)
                return AdvanceResult(None, (await self.state(session_id)).status)
            except BaseException:
                await self._journal.release_step(lease)
                raise
            finally:
                if not cancellation.done():
                    cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)
                if not operation.done():
                    await _cancel_task(operation)
                elif not operation.cancelled():
                    # heartbeat 与提交同时失败时，也要取走提交任务的异常。
                    operation.exception()
                await _stop_heartbeat(heartbeat)
                self._active_decisions.pop(session_id)
                active.finished.set()
            step = step_event.payload.step
            dispatch = await self.dispatcher.start_pending(session_id)
            return AdvanceResult(step, dispatch.status)

    async def _step_heartbeat(self, lease) -> None:
        interval = self._step_lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            renewed = await self._journal.renew_step(
                lease,
                lease_seconds=self._step_lease_seconds,
            )
            if not renewed:
                raise LeaseLostError(lease.token)

    async def state(self, session_id: str) -> CanonicalState:
        events = await self._journal.snapshot(session_id)
        fingerprint = self._event_fingerprint(events)
        journal_position = events[-1].sequence if events else 0
        checkpoint = await self._journal.load_checkpoint(
            session_id,
            journal_position,
            fingerprint,
        )
        if checkpoint is not None:
            return checkpoint
        state = self.projector.project(session_id, events).state
        await self._journal.save_checkpoint(state, fingerprint)
        return state

    async def status(self, session_id: str) -> RuntimeStatus:
        return (await self.state(session_id)).status

    async def trace(self, session_id: str) -> TraceView:
        events = await self._journal.snapshot(session_id)
        return project_trace(session_id, events)

    async def replay(self, session_id: str) -> ReplayView:
        events = await self._journal.snapshot(session_id)
        return replay(session_id, events)

    @staticmethod
    def _event_fingerprint(events: tuple[Event, ...]) -> str:
        content = json.dumps(
            [
                [event.sequence, event.event_id, event.schema_version]
                for event in events
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(content.encode("utf-8")).hexdigest()
