from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from redpanda.runtime.events import (
    Event,
    EventDraft,
)
from redpanda.runtime.model import (
    CanonicalState,
    _require_event_ids,
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
    trigger_event_ids: tuple[str, ...]
    decision_cursor: int
    basis_state_version: str
    observed_journal_position: int

    def __post_init__(self) -> None:
        for label, value in (
            ("session id", self.session_id),
            ("basis state version", self.basis_state_version),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{label} must be a non-empty str")
        _require_event_ids(self.trigger_event_ids)
        if type(self.decision_cursor) is not int or self.decision_cursor < 1:
            raise ValueError("decision cursor must be positive")
        if (
            type(self.observed_journal_position) is not int
            or self.observed_journal_position < 1
        ):
            raise ValueError("observed journal position must be positive")

    @property
    def trigger_event_id(self) -> str:
        return self.trigger_event_ids[0]


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
