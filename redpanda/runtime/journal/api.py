from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from redpanda.runtime.codec import EVENT_SCHEMA_VERSION, delivery_fingerprint
from redpanda.runtime.events import (
    CommandAuthorized,
    CommandOutcomeReceived,
    CommandRejected,
    DecisionCancelled,
    DeliveryIdentity,
    DispatchAttemptStarted,
    DomainFactCommitted,
    Event,
    EventDraft,
    StepContinuationCancelled,
    StepCommitted,
    UserMessageReceived,
)
from redpanda.runtime.model import (
    CanonicalState,
    Command,
)


class DeliveryConflictError(ValueError):
    pass


class AttemptTerminalConflict(ValueError):
    """同一 Attempt 已有终局，且后到的内容不同。先写入的那条保持不变。"""

    def __init__(self, attempt_id: str) -> None:
        self.attempt_id = attempt_id
        super().__init__(f"attempt terminal conflict: {attempt_id}")


class LeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppendResult:
    event: Event
    inserted: bool

    def __post_init__(self) -> None:
        if type(self.event) is not Event:
            raise TypeError("append result event is invalid")
        if type(self.inserted) is not bool:
            raise TypeError("append result inserted must be bool")


@dataclass(frozen=True, slots=True)
class StepClaimRequest:
    session_id: str
    trigger_event_id: str
    decision_cursor: int
    basis_state_version: str
    observed_journal_position: int

    def __post_init__(self) -> None:
        for label, value in (
            ("session id", self.session_id),
            ("trigger event id", self.trigger_event_id),
            ("basis state version", self.basis_state_version),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{label} must be a non-empty str")
        if type(self.decision_cursor) is not int or self.decision_cursor < 1:
            raise ValueError("decision cursor must be positive")
        if (
            type(self.observed_journal_position) is not int
            or self.observed_journal_position < 1
        ):
            raise ValueError("observed journal position must be positive")


@dataclass(frozen=True, slots=True)
class StepLease:
    request: StepClaimRequest
    token: str
    owner_id: str
    generation: int
    expires_at: float

    def __post_init__(self) -> None:
        if type(self.request) is not StepClaimRequest:
            raise TypeError("step lease request is invalid")
        for label, value in (("token", self.token), ("owner id", self.owner_id)):
            if type(value) is not str or not value:
                raise ValueError(f"step lease {label} must be a non-empty str")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("step lease generation must be positive")
        if isinstance(self.expires_at, bool) or not isinstance(
            self.expires_at, (int, float)
        ):
            raise TypeError("step lease expires_at must be numeric")


class Journal(Protocol):
    async def create_session(self, session_id: str) -> bool: ...

    async def session_exists(self, session_id: str) -> bool: ...

    async def append(self, draft: EventDraft) -> Event: ...

    async def accept_delivery(self, draft: EventDraft) -> AppendResult: ...

    async def snapshot(self, session_id: str) -> tuple[Event, ...]: ...

    async def acquire_step(
        self,
        request: StepClaimRequest,
        *,
        token: str,
        owner_id: str,
        lease_seconds: float,
    ) -> StepLease | None: ...

    async def release_step(self, lease: StepLease) -> None: ...

    async def renew_step(
        self,
        lease: StepLease,
        *,
        lease_seconds: float,
    ) -> bool: ...

    async def commit_step(
        self,
        lease: StepLease,
        draft: EventDraft,
    ) -> Event: ...

    async def cancel_decision(
        self,
        draft: EventDraft,
    ) -> Event | None: ...

    async def cancel_continuation(
        self,
        draft: EventDraft,
    ) -> Event | None: ...

    async def start_attempt(
        self,
        draft: EventDraft,
        *,
        lease_seconds: float = 30.0,
    ) -> Event | None: ...

    async def grant_command(self, draft: EventDraft) -> Event | None: ...

    async def reject_command(self, draft: EventDraft) -> Event | None: ...

    async def renew_attempt(
        self,
        attempt_id: str,
        claim_token: str,
        *,
        lease_seconds: float,
    ) -> bool: ...

    async def release_attempt(
        self,
        attempt_id: str,
        claim_token: str,
    ) -> None: ...

    async def record_attempt_fact(
        self,
        draft: EventDraft,
    ) -> Event | None: ...

    async def load_checkpoint(
        self,
        session_id: str,
        journal_position: int,
        fingerprint: str,
    ) -> CanonicalState | None: ...

    async def save_checkpoint(
        self,
        state: CanonicalState,
        fingerprint: str,
    ) -> None: ...

    async def delete_checkpoint(self, session_id: str) -> None: ...

class MemoryJournal:
    def __init__(
        self,
        events: Iterable[Event] = (),
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._events: dict[str, list[Event]] = {}
        self._event_ids: dict[str, Event] = {}
        self._deliveries: dict[
            DeliveryIdentity,
            tuple[Event, str],
        ] = {}
        self._step_claims: dict[str, StepLease] = {}
        self._step_claim_tokens: dict[str, str] = {}
        self._decision_consumptions: dict[str, str] = {}
        self._cancelled_step_continuations: dict[str, str] = {}
        self._step_ids: set[str] = set()
        self._commands: dict[str, tuple[str, str]] = {}
        self._command_definitions: dict[str, Command] = {}
        self._dispatch_eligibility: dict[str, str] = {}
        self._rejected_commands: dict[str, str] = {}
        self._attempts: dict[tuple[str, int], Event] = {}
        self._attempt_ids: dict[str, Event] = {}
        self._attempt_claim_tokens: dict[str, Event] = {}
        self._attempt_leases: dict[str, tuple[str, float]] = {}
        self._attempt_terminal_events: dict[str, Event] = {}
        self._terminal_commands: set[str] = set()
        self._checkpoints: dict[str, tuple[int, str, CanonicalState]] = {}
        self._clock = clock
        self._lock = asyncio.Lock()
        for event in events:
            self._restore(event)

    async def create_session(self, session_id: str) -> bool:
        self._validate_session_id(session_id)
        async with self._lock:
            if session_id in self._events:
                return False
            self._events[session_id] = []
            return True

    async def session_exists(self, session_id: str) -> bool:
        self._validate_session_id(session_id)
        async with self._lock:
            return session_id in self._events

    def _restore(self, event: Event) -> None:
        if type(event) is not Event:
            raise TypeError("restored event must be Event")
        if event.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported event schema version: {event.schema_version}"
            )
        is_external = isinstance(
            event.payload,
            (
                UserMessageReceived,
                DomainFactCommitted,
            ),
        )
        if is_external != (event.delivery is not None):
            raise ValueError("restored event delivery boundary is invalid")
        if event.event_id in self._event_ids:
            raise ValueError(f"duplicate event id: {event.event_id}")
        session_events = self._events.setdefault(event.session_id, [])
        expected = len(session_events) + 1
        if event.sequence != expected:
            raise ValueError(
                f"invalid sequence for {event.session_id}: "
                f"expected {expected}, got {event.sequence}"
            )
        if event.delivery is not None:
            existing = self._deliveries.get(event.delivery)
            if existing is not None:
                raise ValueError(f"duplicate delivery: {event.delivery}")
        self._prevalidate_index(event)
        session_events.append(event)
        self._event_ids[event.event_id] = event
        self._index_event(event)
        if event.delivery is not None:
            self._deliveries[event.delivery] = (
                event,
                self._event_delivery_fingerprint(event),
            )

    async def append(self, draft: EventDraft) -> Event:
        self._validate_generic_append(draft)
        async with self._lock:
            return self._append_locked(draft).event

    async def accept_delivery(self, draft: EventDraft) -> AppendResult:
        self._validate_external_delivery(draft)
        if draft.delivery is None:
            raise ValueError("external event requires delivery identity")
        async with self._lock:
            receipt = self._deliveries.get(draft.delivery)
            if receipt is not None:
                existing, fingerprint = receipt
                if fingerprint != delivery_fingerprint(draft):
                    raise DeliveryConflictError(
                        f"delivery content conflict: {draft.delivery}"
                    )
                return AppendResult(existing, False)
            result = self._append_locked(draft)
            self._deliveries[draft.delivery] = (
                result.event,
                delivery_fingerprint(draft),
            )
            return result

    async def snapshot(self, session_id: str) -> tuple[Event, ...]:
        async with self._lock:
            return tuple(self._events.get(session_id, ()))

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if type(session_id) is not str:
            raise TypeError("session id must be str")
        if not session_id:
            raise ValueError("session id must not be empty")

    async def acquire_step(
        self,
        request: StepClaimRequest,
        *,
        token: str,
        owner_id: str,
        lease_seconds: float,
    ) -> StepLease | None:
        if not token or not owner_id:
            raise ValueError("step claim identity must not be empty")
        if lease_seconds <= 0:
            raise ValueError("step lease duration must be positive")
        async with self._lock:
            trigger = self._event_ids.get(request.trigger_event_id)
            if trigger is None or trigger.session_id != request.session_id:
                raise KeyError(request.trigger_event_id)
            if request.trigger_event_id in self._decision_consumptions:
                return None
            current = self._step_claims.get(request.session_id)
            now = self._clock()
            if current is not None and current.expires_at > now:
                if current.token == token and current.request == request:
                    return current
                return None
            token_session_id = self._step_claim_tokens.get(token)
            if token_session_id is not None and token_session_id != request.session_id:
                raise ValueError(f"duplicate step claim token: {token}")
            if current is not None:
                if self._step_claim_tokens.get(current.token) == request.session_id:
                    self._step_claim_tokens.pop(current.token, None)
            generation = current.generation + 1 if current is not None else 1
            lease = StepLease(
                request=request,
                token=token,
                owner_id=owner_id,
                generation=generation,
                expires_at=now + lease_seconds,
            )
            self._step_claims[request.session_id] = lease
            self._step_claim_tokens[token] = request.session_id
            return lease

    async def release_step(self, lease: StepLease) -> None:
        async with self._lock:
            current = self._step_claims.get(lease.request.session_id)
            if self._same_step_lease_identity(current, lease):
                del self._step_claims[lease.request.session_id]
                if self._step_claim_tokens.get(lease.token) == lease.request.session_id:
                    self._step_claim_tokens.pop(lease.token, None)

    async def renew_step(
        self,
        lease: StepLease,
        *,
        lease_seconds: float,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("step lease duration must be positive")
        async with self._lock:
            current = self._step_claims.get(lease.request.session_id)
            now = self._clock()
            if (
                not self._same_step_lease_identity(current, lease)
            ):
                return False
            self._step_claims[lease.request.session_id] = replace(
                current,
                expires_at=now + lease_seconds,
            )
            return True

    async def commit_step(
        self,
        lease: StepLease,
        draft: EventDraft,
    ) -> Event:
        self._validate_internal_draft(draft)
        async with self._lock:
            existing = self._event_ids.get(draft.event_id)
            if existing is not None:
                if not self._same_event(existing, draft):
                    raise ValueError(f"event id conflict: {draft.event_id}")
                if (
                    self._decision_consumptions.get(lease.request.trigger_event_id)
                    != existing.event_id
                ):
                    raise LeaseLostError(lease.token)
                return existing
            self._validate_step_lease(lease, draft)
            event = self._append_locked(draft).event
            del self._step_claims[lease.request.session_id]
            if self._step_claim_tokens.get(lease.token) == lease.request.session_id:
                self._step_claim_tokens.pop(lease.token, None)
            return event

    async def cancel_decision(
        self,
        draft: EventDraft,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, DecisionCancelled):
            raise TypeError(type(payload).__name__)
        async with self._lock:
            existing = self._event_ids.get(draft.event_id)
            if existing is not None:
                if not self._same_event(existing, draft):
                    raise ValueError(f"event id conflict: {draft.event_id}")
                if self._decision_consumptions.get(payload.trigger_event_id) != existing.event_id:
                    return None
                return existing
            if payload.trigger_event_id in self._decision_consumptions:
                return None
            return self._append_locked(draft).event

    async def cancel_continuation(
        self,
        draft: EventDraft,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, StepContinuationCancelled):
            raise TypeError(type(payload).__name__)
        async with self._lock:
            existing = self._event_ids.get(draft.event_id)
            if existing is not None:
                if not self._same_event(existing, draft):
                    raise ValueError(f"event id conflict: {draft.event_id}")
                if self._cancelled_step_continuations.get(payload.step_event_id) != existing.event_id:
                    return None
                return existing
            if payload.step_event_id in self._cancelled_step_continuations:
                return None
            return self._append_locked(draft).event

    async def start_attempt(
        self,
        draft: EventDraft,
        *,
        lease_seconds: float = 30.0,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, DispatchAttemptStarted):
            raise TypeError(type(payload).__name__)
        if lease_seconds <= 0:
            raise ValueError("attempt lease duration must be positive")
        async with self._lock:
            key = (payload.command_id, payload.attempt_number)
            if key in self._attempts:
                return None
            existing_attempt = self._attempt_ids.get(payload.attempt_id)
            if existing_attempt is not None:
                existing_payload = existing_attempt.payload
                if (
                    existing_payload.command_id == payload.command_id
                    and existing_payload.attempt_number == payload.attempt_number
                ):
                    return None
                raise ValueError(f"duplicate attempt id: {payload.attempt_id}")
            if payload.claim_token in self._attempt_claim_tokens:
                raise ValueError(
                    f"duplicate attempt claim token: {payload.claim_token}"
                )
            command = self._commands.get(payload.command_id)
            if command is None or command[0] != draft.session_id:
                raise KeyError(payload.command_id)
            if payload.command_id in self._terminal_commands:
                return None
            expected_cause = self._dispatch_eligibility.get(payload.command_id)
            if expected_cause is None or draft.causation_id != expected_cause:
                return None
            if payload.attempt_number != self._attempt_count(payload.command_id) + 1:
                return None
            return self._append_locked(
                draft,
                attempt_lease_expires_at=self._clock() + lease_seconds,
            ).event

    async def grant_command(self, draft: EventDraft) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, CommandAuthorized):
            raise TypeError(type(payload).__name__)
        async with self._lock:
            causation_id = self._authorization_causation_id(
                draft.session_id,
                payload.command_id,
            )
            if causation_id is None:
                return None
            return self._append_locked(
                replace(draft, causation_id=causation_id)
            ).event

    async def reject_command(self, draft: EventDraft) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, CommandRejected):
            raise TypeError(type(payload).__name__)
        async with self._lock:
            causation_id = self._authorization_causation_id(
                draft.session_id,
                payload.command_id,
            )
            if causation_id is None:
                return None
            return self._append_locked(
                replace(draft, causation_id=causation_id)
            ).event

    def _authorization_causation_id(
        self,
        session_id: str,
        command_id: str,
    ) -> str | None:
        command = self._commands.get(command_id)
        if command is None or command[0] != session_id:
            raise KeyError(command_id)
        issued_event_id = command[1]
        if command_id in self._terminal_commands:
            return None
        if command_id in self._rejected_commands:
            return None
        if command_id in self._dispatch_eligibility:
            return None
        if self._attempt_count(command_id) != 0:
            return None
        return issued_event_id

    async def renew_attempt(
        self,
        attempt_id: str,
        claim_token: str,
        *,
        lease_seconds: float,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("attempt lease duration must be positive")
        async with self._lock:
            current = self._attempt_leases.get(attempt_id)
            now = self._clock()
            if current is None or current[0] != claim_token:
                return False
            self._attempt_leases[attempt_id] = (
                claim_token,
                now + lease_seconds,
            )
            return True

    async def release_attempt(
        self,
        attempt_id: str,
        claim_token: str,
    ) -> None:
        async with self._lock:
            current = self._attempt_leases.get(attempt_id)
            if current is not None and current[0] == claim_token:
                self._attempt_leases.pop(attempt_id, None)

    async def record_attempt_fact(
        self,
        draft: EventDraft,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, CommandOutcomeReceived):
            raise TypeError(type(payload).__name__)
        if payload.attempt_id is None:
            raise ValueError("attempt fact requires attempt identity")
        async with self._lock:
            existing = self._event_ids.get(draft.event_id)
            if existing is not None:
                if not self._same_event(existing, draft):
                    raise ValueError(f"event id conflict: {draft.event_id}")
                return existing
            attempt_id = payload.attempt_id
            terminal = self._attempt_terminal_events.get(attempt_id)
            if terminal is not None:
                if terminal.payload != payload:
                    raise AttemptTerminalConflict(attempt_id)
                return terminal
            attempt_event = self._attempt_ids.get(attempt_id)
            if attempt_event is None:
                raise KeyError(attempt_id)
            attempt = attempt_event.payload
            if attempt.command_id != payload.command_id:
                raise ValueError("attempt fact command mismatch")
            if draft.causation_id != attempt_event.event_id:
                return None
            return self._append_locked(draft).event

    async def load_checkpoint(
        self,
        session_id: str,
        journal_position: int,
        fingerprint: str,
    ) -> CanonicalState | None:
        async with self._lock:
            checkpoint = self._checkpoints.get(session_id)
            if checkpoint is None:
                return None
            position, stored_fingerprint, state = checkpoint
            if position != journal_position or stored_fingerprint != fingerprint:
                return None
            return state

    async def save_checkpoint(
        self,
        state: CanonicalState,
        fingerprint: str,
    ) -> None:
        async with self._lock:
            self._checkpoints[state.session_id] = (
                state.journal_position,
                fingerprint,
                state,
            )

    async def delete_checkpoint(self, session_id: str) -> None:
        async with self._lock:
            self._checkpoints.pop(session_id, None)

    def _append_locked(
        self,
        draft: EventDraft,
        *,
        attempt_lease_expires_at: float = 0.0,
    ) -> AppendResult:
        if draft.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported event schema version: {draft.schema_version}"
            )
        existing = self._event_ids.get(draft.event_id)
        if existing is not None:
            if not self._same_event(existing, draft):
                raise ValueError(f"event id conflict: {draft.event_id}")
            return AppendResult(existing, False)
        payload = draft.payload
        if (
            isinstance(payload, CommandOutcomeReceived)
            and payload.attempt_id is not None
        ):
            terminal = self._attempt_terminal_events.get(payload.attempt_id)
            if terminal is not None:
                if terminal.payload != payload:
                    raise AttemptTerminalConflict(payload.attempt_id)
                return AppendResult(terminal, False)
        session_events = self._events.setdefault(draft.session_id, [])
        event = Event(
            event_id=draft.event_id,
            session_id=draft.session_id,
            sequence=len(session_events) + 1,
            payload=draft.payload,
            occurred_at=draft.occurred_at,
            causation_id=draft.causation_id,
            correlation_id=draft.correlation_id,
            schema_version=draft.schema_version,
            artifact_refs=draft.artifact_refs,
            delivery=draft.delivery,
        )
        self._prevalidate_index(event)
        session_events.append(event)
        self._event_ids[event.event_id] = event
        self._index_event(
            event,
            attempt_lease_expires_at=attempt_lease_expires_at,
        )
        return AppendResult(event, True)

    def _index_event(
        self,
        event: Event,
        *,
        attempt_lease_expires_at: float = 0.0,
    ) -> None:
        payload = event.payload
        if isinstance(payload, StepCommitted):
            step = payload.step
            self._decision_consumptions[step.trigger_event_id] = event.event_id
            self._step_ids.add(step.step_id)
            for command in step.commands:
                self._commands[command.command_id] = (
                    event.session_id,
                    event.event_id,
                )
                self._command_definitions[command.command_id] = command
                if not command.requires_authorization:
                    self._dispatch_eligibility[command.command_id] = event.event_id
        elif isinstance(payload, CommandAuthorized):
            if payload.command_id in self._dispatch_eligibility:
                raise ValueError(f"command already authorized: {payload.command_id}")
            if payload.command_id in self._rejected_commands:
                raise ValueError(f"command already rejected: {payload.command_id}")
            self._dispatch_eligibility[payload.command_id] = event.event_id
        elif isinstance(payload, CommandRejected):
            if payload.command_id in self._rejected_commands:
                raise ValueError(f"command already rejected: {payload.command_id}")
            if payload.command_id in self._dispatch_eligibility:
                raise ValueError(
                    f"cannot reject authorized command: {payload.command_id}"
                )
            self._rejected_commands[payload.command_id] = event.event_id
        elif isinstance(payload, DispatchAttemptStarted):
            self._attempts[
                (
                    payload.command_id,
                    payload.attempt_number,
                )
            ] = event
            self._attempt_ids[payload.attempt_id] = event
            self._attempt_claim_tokens[payload.claim_token] = event
            self._attempt_leases[payload.attempt_id] = (
                payload.claim_token,
                attempt_lease_expires_at,
            )
            self._dispatch_eligibility.pop(payload.command_id, None)
        elif isinstance(payload, CommandOutcomeReceived):
            if payload.attempt_id is not None:
                self._attempt_terminal_events[payload.attempt_id] = event
                self._clear_attempt_claims(payload.attempt_id)
            self._terminal_commands.add(payload.command_id)
            self._dispatch_eligibility.pop(payload.command_id, None)
        elif isinstance(payload, DecisionCancelled):
            self._decision_consumptions[payload.trigger_event_id] = event.event_id
            self._clear_session_step_claim(event.session_id)
        elif isinstance(payload, StepContinuationCancelled):
            self._cancelled_step_continuations[payload.step_event_id] = event.event_id

    def _clear_attempt_claims(self, attempt_id: str) -> None:
        self._attempt_leases.pop(attempt_id, None)

    def _prevalidate_index(self, event: Event) -> None:
        payload = event.payload
        if isinstance(payload, StepCommitted):
            step = payload.step
            if step.step_id in self._step_ids:
                raise ValueError(f"duplicate step id: {step.step_id}")
            if step.trigger_event_id in self._decision_consumptions:
                raise ValueError(
                    f"decision event consumed twice: {step.trigger_event_id}"
                )
            duplicate = next(
                (
                    command.command_id
                    for command in step.commands
                    if command.command_id in self._commands
                ),
                None,
            )
            if duplicate is not None:
                raise ValueError(f"duplicate command id: {duplicate}")
        elif isinstance(payload, (CommandAuthorized, CommandRejected)):
            if payload.command_id not in self._commands:
                raise KeyError(payload.command_id)
        elif isinstance(payload, DispatchAttemptStarted):
            if (payload.command_id, payload.attempt_number) in self._attempts:
                raise ValueError("duplicate command attempt")
            if payload.attempt_id in self._attempt_ids:
                raise ValueError(f"duplicate attempt id: {payload.attempt_id}")
            if payload.claim_token in self._attempt_claim_tokens:
                raise ValueError(
                    f"duplicate attempt claim token: {payload.claim_token}"
                )
        elif isinstance(payload, CommandOutcomeReceived):
            if payload.command_id not in self._commands:
                raise KeyError(payload.command_id)
            if (
                payload.attempt_id is not None
                and payload.attempt_id in self._attempt_terminal_events
            ):
                raise ValueError(f"attempt already terminal: {payload.attempt_id}")
        elif isinstance(payload, DecisionCancelled):
            trigger = self._event_ids.get(payload.trigger_event_id)
            if trigger is None or trigger.session_id != event.session_id:
                raise KeyError(payload.trigger_event_id)
            if event.causation_id != payload.trigger_event_id:
                raise ValueError("decision cancellation causation mismatch")
            if payload.trigger_event_id in self._decision_consumptions:
                raise ValueError(
                    f"decision event consumed twice: {payload.trigger_event_id}"
                )
        elif isinstance(payload, StepContinuationCancelled):
            step_event = self._event_ids.get(payload.step_event_id)
            if step_event is None or step_event.session_id != event.session_id:
                raise KeyError(payload.step_event_id)
            if not isinstance(step_event.payload, StepCommitted):
                raise ValueError(
                    f"continuation target is not a step: {payload.step_event_id}"
                )
            if event.causation_id != payload.step_event_id:
                raise ValueError("continuation cancellation causation mismatch")
            if payload.step_event_id in self._cancelled_step_continuations:
                raise ValueError(
                    f"step continuation cancelled twice: {payload.step_event_id}"
                )

    def _clear_session_step_claim(self, session_id: str) -> None:
        current = self._step_claims.pop(session_id, None)
        if current is None:
            return
        if self._step_claim_tokens.get(current.token) == session_id:
            self._step_claim_tokens.pop(current.token, None)

    def _validate_step_lease(
        self,
        lease: StepLease,
        draft: EventDraft,
    ) -> None:
        current = self._step_claims.get(lease.request.session_id)
        if (
            not self._same_step_lease_identity(current, lease)
        ):
            raise LeaseLostError(lease.token)
        payload = draft.payload
        if not isinstance(payload, StepCommitted):
            raise TypeError(type(payload).__name__)
        step = payload.step
        request = lease.request
        if (
            draft.session_id != request.session_id
            or step.trigger_event_id != request.trigger_event_id
            or step.decision_cursor != request.decision_cursor
            or step.basis_state_version != request.basis_state_version
            or step.observed_journal_position != request.observed_journal_position
            or draft.causation_id != request.trigger_event_id
        ):
            raise ValueError("step does not match its claim")
        if request.trigger_event_id in self._decision_consumptions:
            raise LeaseLostError(lease.token)

    @staticmethod
    def _same_step_lease_identity(
        left: StepLease | None,
        right: StepLease,
    ) -> bool:
        return (
            left is not None
            and left.request == right.request
            and left.token == right.token
            and left.owner_id == right.owner_id
            and left.generation == right.generation
        )

    def _attempt_count(self, command_id: str) -> int:
        return sum(1 for key in self._attempts if key[0] == command_id)

    @staticmethod
    def _same_delivery(event: Event, draft: EventDraft) -> bool:
        return (
            event.session_id == draft.session_id
            and event.payload == draft.payload
            and event.causation_id == draft.causation_id
            and event.correlation_id == draft.correlation_id
            and event.schema_version == draft.schema_version
            and event.artifact_refs == draft.artifact_refs
        )

    @staticmethod
    def _same_event(event: Event, draft: EventDraft) -> bool:
        return (
            MemoryJournal._same_delivery(event, draft)
            and event.delivery == draft.delivery
        )

    @staticmethod
    def _event_delivery_fingerprint(event: Event) -> str:
        return delivery_fingerprint(
            EventDraft(
                event_id=event.event_id,
                session_id=event.session_id,
                payload=event.payload,
                occurred_at=event.occurred_at,
                causation_id=event.causation_id,
                correlation_id=event.correlation_id,
                schema_version=event.schema_version,
                artifact_refs=event.artifact_refs,
                delivery=event.delivery,
            )
        )

    @staticmethod
    def _validate_generic_append(draft: EventDraft) -> None:
        if draft.delivery is not None:
            raise ValueError("delivery events must use accept_delivery")
        if isinstance(draft.payload, DomainFactCommitted):
            raise ValueError("domain fact requires delivery identity")
        if isinstance(
            draft.payload,
            (
                StepCommitted,
                CommandAuthorized,
                CommandRejected,
                DecisionCancelled,
                StepContinuationCancelled,
                DispatchAttemptStarted,
                UserMessageReceived,
            ),
        ):
            raise ValueError("conditional event requires its Journal method")
        if isinstance(draft.payload, CommandOutcomeReceived):
            raise ValueError("attempt fact requires conditional append")

    @staticmethod
    def _validate_external_delivery(draft: EventDraft) -> None:
        if not isinstance(
            draft.payload,
            (UserMessageReceived, DomainFactCommitted),
        ):
            raise ValueError("delivery payload is not an external event")

    @staticmethod
    def _validate_internal_draft(draft: EventDraft) -> None:
        if draft.delivery is not None:
            raise ValueError("internal event cannot have delivery identity")
