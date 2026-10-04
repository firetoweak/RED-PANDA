from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TypeAlias

from redpanda.runtime.model import CommandOutcome, Step, freeze_value


def _require_str_tuple(value: object, name: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{name} must be tuple")
    if any(type(item) is not str or not item for item in value):
        raise ValueError(f"{name} are invalid")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} contain duplicates")


MAX_EVENT_PAYLOAD_BYTES = 256 * 1024
MAX_EVENT_PAYLOAD_DEPTH = 48


def _require_str(value: object, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be str")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _require_optional_str(value: object, name: str) -> None:
    if value is not None:
        _require_str(value, name)


@dataclass(frozen=True, slots=True)
class DeliveryIdentity:
    source: str
    delivery_id: str

    def __post_init__(self) -> None:
        _require_str(self.source, "delivery source")
        _require_str(self.delivery_id, "delivery id")


def _payload_size(value: object, depth: int = 0) -> int:
    if depth > MAX_EVENT_PAYLOAD_DEPTH:
        raise ValueError("event payload exceeds maximum depth")
    if value is None:
        return 4
    if isinstance(value, Enum):
        return _payload_size(value.value, depth + 1)
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 2
    if isinstance(value, (bool, int, float)):
        return len(str(value).encode("ascii"))
    if isinstance(value, Mapping):
        return 2 + sum(
            len(key.encode("utf-8")) + _payload_size(item, depth + 1) + 3
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return 2 + sum(_payload_size(item, depth + 1) + 1 for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return 2 + sum(
            len(field.name.encode("utf-8"))
            + _payload_size(getattr(value, field.name), depth + 1)
            + 3
            for field in fields(value)
        )
    raise TypeError(f"event payload value is unsupported: {type(value).__name__}")


def _validate_payload_size(payload: object) -> None:
    if _payload_size(payload) > MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("event payload exceeds maximum size")


def _validate_envelope(
    event_id: str,
    session_id: str,
    occurred_at: datetime,
    schema_version: int,
) -> None:
    _require_str(event_id, "event id")
    _require_str(session_id, "session id")
    if type(occurred_at) is not datetime:
        raise TypeError("occurred_at must be datetime")
    if occurred_at.tzinfo is not timezone.utc:
        raise ValueError("occurred_at must use UTC")
    if type(schema_version) is not int or schema_version < 1:
        raise ValueError("schema version must be positive")


def _validate_event_metadata(
    causation_id: str | None,
    correlation_id: str | None,
    artifact_refs: tuple[str, ...],
    delivery: DeliveryIdentity | None,
) -> None:
    _require_optional_str(causation_id, "causation id")
    _require_optional_str(correlation_id, "correlation id")
    if type(artifact_refs) is not tuple:
        raise TypeError("artifact refs must be tuple")
    for artifact_ref in artifact_refs:
        _require_str(artifact_ref, "artifact ref")
    if delivery is not None and type(delivery) is not DeliveryIdentity:
        raise TypeError("delivery must be DeliveryIdentity")


@dataclass(frozen=True, slots=True)
class UserMessageReceived:
    content: str

    def __post_init__(self) -> None:
        _require_str(self.content, "user message")


@dataclass(frozen=True, slots=True)
class StepCommitted:
    step: Step
    decision_metadata: object = None

    def __post_init__(self) -> None:
        if type(self.step) is not Step:
            raise TypeError("step must be Step")
        object.__setattr__(self, "decision_metadata", freeze_value(self.decision_metadata))


@dataclass(frozen=True, slots=True)
class CommandAuthorized:
    command_id: str

    def __post_init__(self) -> None:
        _require_str(self.command_id, "command id")


@dataclass(frozen=True, slots=True)
class CommandRejected:
    command_id: str

    def __post_init__(self) -> None:
        _require_str(self.command_id, "command id")


@dataclass(frozen=True, slots=True)
class DispatchAttemptStarted:
    attempt_id: str
    command_id: str
    attempt_number: int = 1
    claim_token: str = "local"
    worker_id: str = "local"

    def __post_init__(self) -> None:
        _require_str(self.attempt_id, "attempt id")
        _require_str(self.command_id, "command id")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt number must be positive")
        _require_str(self.claim_token, "attempt claim token")
        _require_str(self.worker_id, "attempt worker id")


@dataclass(frozen=True, slots=True)
class CommandOutcomeReceived:
    command_id: str
    attempt_id: str | None
    outcome: CommandOutcome

    def __post_init__(self) -> None:
        _require_str(self.command_id, "command id")
        _require_optional_str(self.attempt_id, "attempt id")
        if type(self.outcome) is not CommandOutcome:
            raise TypeError("outcome must be CommandOutcome")


@dataclass(frozen=True, slots=True)
class DecisionCancelled:
    trigger_event_id: str

    def __post_init__(self) -> None:
        _require_str(self.trigger_event_id, "cancelled decision trigger")


@dataclass(frozen=True, slots=True)
class StepContinuationCancelled:
    step_event_id: str

    def __post_init__(self) -> None:
        _require_str(self.step_event_id, "cancelled step continuation")


@dataclass(frozen=True, slots=True)
class DomainFactCommitted:
    fact_type: str
    data: object
    requests_decision: bool = False

    def __post_init__(self) -> None:
        _require_str(self.fact_type, "domain fact type")
        if type(self.requests_decision) is not bool:
            raise TypeError("requests_decision must be bool")
        object.__setattr__(self, "data", freeze_value(self.data))


EventPayload: TypeAlias = (
    UserMessageReceived
    | StepCommitted
    | CommandAuthorized
    | CommandRejected
    | DispatchAttemptStarted
    | CommandOutcomeReceived
    | DecisionCancelled
    | StepContinuationCancelled
    | DomainFactCommitted
)

_EVENT_PAYLOAD_TYPES = (
    UserMessageReceived,
    StepCommitted,
    CommandAuthorized,
    CommandRejected,
    DispatchAttemptStarted,
    CommandOutcomeReceived,
    DecisionCancelled,
    StepContinuationCancelled,
    DomainFactCommitted,
)


@dataclass(frozen=True, slots=True)
class EventDraft:
    event_id: str
    session_id: str
    payload: EventPayload
    occurred_at: datetime
    causation_id: str | None = None
    correlation_id: str | None = None
    schema_version: int = 5
    artifact_refs: tuple[str, ...] = ()
    delivery: DeliveryIdentity | None = None

    def __post_init__(self) -> None:
        _validate_envelope(
            self.event_id,
            self.session_id,
            self.occurred_at,
            self.schema_version,
        )
        if type(self.payload) not in _EVENT_PAYLOAD_TYPES:
            raise TypeError("event payload type is invalid")
        _validate_event_metadata(
            self.causation_id,
            self.correlation_id,
            self.artifact_refs,
            self.delivery,
        )
        _validate_payload_size(self.payload)


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    session_id: str
    sequence: int
    payload: EventPayload
    occurred_at: datetime
    causation_id: str | None
    correlation_id: str | None
    schema_version: int
    artifact_refs: tuple[str, ...]
    delivery: DeliveryIdentity | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("event sequence must be positive")
        _validate_envelope(
            self.event_id,
            self.session_id,
            self.occurred_at,
            self.schema_version,
        )
        if type(self.payload) not in _EVENT_PAYLOAD_TYPES:
            raise TypeError("event payload type is invalid")
        _validate_event_metadata(
            self.causation_id,
            self.correlation_id,
            self.artifact_refs,
            self.delivery,
        )
        _validate_payload_size(self.payload)

    @property
    def kind(self) -> str:
        return type(self.payload).__name__
