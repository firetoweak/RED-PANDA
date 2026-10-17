from __future__ import annotations

import json
from redpanda.runtime.json_values import thaw_value
from hashlib import sha256

from redpanda.runtime.events import (
    CommandAuthorized,
    CommandOutcomeReceived,
    CommandRejected,
    DecisionCancelled,
    DomainFactCommitted,
    DispatchAttemptStarted,
    EventDraft,
    EventPayload,
    StepContinuationCancelled,
    StepCommitted,
    UserMessageReceived,
)
from redpanda.runtime.model import (
    AttemptPhase,
    AttemptState,
    CanonicalState,
    Command,
    CommandOutcome,
    CommandPhase,
    CommandState,
    InvokeTool,
    ModelDecision,
    OutcomeStatus,
    RuntimeStatus,
    Step,
    StepState,
)


EVENT_SCHEMA_VERSION = 5
DELIVERY_FINGERPRINT_VERSION = 3
STATE_CODEC_VERSION = 6
STATE_PROJECTION_VERSION = "canonical-state-v6"

_USER_MESSAGE = "user.message.received"
_STEP_COMMITTED = "step.committed"
_COMMAND_AUTHORIZED = "command.authorized"
_COMMAND_REJECTED = "command.rejected"
_ATTEMPT_STARTED = "command.attempt.started"
_OUTCOME_RECEIVED = "command.outcome.received"
_DECISION_CANCELLED = "decision.cancelled"
_STEP_CONTINUATION_CANCELLED = "step.continuation.cancelled"
_DOMAIN_FACT_COMMITTED = "domain.fact.committed"


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _effect_to_data(effect: InvokeTool) -> dict[str, object]:
    return {
        "type": "invoke_tool",
        "name": effect.name,
        "arguments": [[key, thaw_value(value)] for key, value in effect.arguments],
    }


def _effect_from_data(data: dict[str, object]) -> InvokeTool:
    kind = data["type"]
    if kind == "invoke_tool":
        return InvokeTool(
            name=data["name"],
            arguments=tuple((item[0], item[1]) for item in data["arguments"]),
        )
    raise ValueError(f"unknown command effect: {kind}")


def _outcome_to_data(outcome: CommandOutcome) -> dict[str, object]:
    return {
        "status": outcome.status.value,
        "value": thaw_value(outcome.value),
        "error_type": outcome.error_type,
        "error_message": outcome.error_message,
    }


def _outcome_from_data(data: dict[str, object]) -> CommandOutcome:
    return CommandOutcome(
        status=OutcomeStatus(data["status"]),
        value=data["value"],
        error_type=data["error_type"],
        error_message=data["error_message"],
    )


def _command_to_data(command: Command) -> dict[str, object]:
    return {
        "command_id": command.command_id,
        "effect": _effect_to_data(command.effect),
        "requires_authorization": command.requires_authorization,
        "decision_on_outcome": command.decision_on_outcome,
    }


def _command_from_data(data: dict[str, object]) -> Command:
    return Command(
        command_id=data["command_id"],
        effect=_effect_from_data(data["effect"]),
        requires_authorization=data["requires_authorization"],
        decision_on_outcome=data["decision_on_outcome"],
    )


def _decision_to_data(decision: ModelDecision) -> dict[str, object]:
    return {
        "content": decision.content,
        "command_requests": [
            _effect_to_data(effect) for effect in decision.command_requests
        ],
    }


def _decision_from_data(data: dict[str, object]) -> ModelDecision:
    return ModelDecision(
        content=data["content"],
        command_requests=tuple(
            _effect_from_data(effect) for effect in data["command_requests"]
        ),
    )


def _step_to_data(step: Step) -> dict[str, object]:
    return {
        "step_id": step.step_id,
        "trigger_event_id": step.trigger_event_id,
        "decision_cursor": step.decision_cursor,
        "basis_state_version": step.basis_state_version,
        "observed_journal_position": step.observed_journal_position,
        "decision": _decision_to_data(step.decision),
        "commands": [_command_to_data(command) for command in step.commands],
    }


def _step_from_data(data: dict[str, object]) -> Step:
    return Step(
        step_id=data["step_id"],
        trigger_event_id=data["trigger_event_id"],
        decision_cursor=data["decision_cursor"],
        basis_state_version=data["basis_state_version"],
        observed_journal_position=data["observed_journal_position"],
        decision=_decision_from_data(data["decision"]),
        commands=tuple(_command_from_data(command) for command in data["commands"]),
    )


def _step_state_to_data(state: StepState) -> dict[str, object]:
    return {
        "step": _step_to_data(state.step),
        "committed_event_id": state.committed_event_id,
        "sequence": state.sequence,
        "decision_metadata": thaw_value(state.decision_metadata),
        "commands": [_command_state_to_data(command) for command in state.commands],
    }


def _step_state_from_data(data: dict[str, object]) -> StepState:
    return StepState(
        step=_step_from_data(data["step"]),
        committed_event_id=data["committed_event_id"],
        sequence=data["sequence"],
        decision_metadata=data["decision_metadata"],
        commands=tuple(
            _command_state_from_data(command) for command in data["commands"]
        ),
    )


def encode_payload(payload: EventPayload) -> tuple[str, str]:
    if isinstance(payload, UserMessageReceived):
        kind = _USER_MESSAGE
        data = {"content": payload.content}
    elif isinstance(payload, StepCommitted):
        kind = _STEP_COMMITTED
        data = {"step": _step_to_data(payload.step), "decision_metadata": thaw_value(payload.decision_metadata)}
    elif isinstance(payload, CommandAuthorized):
        kind = _COMMAND_AUTHORIZED
        data = {"command_id": payload.command_id}
    elif isinstance(payload, CommandRejected):
        kind = _COMMAND_REJECTED
        data = {"command_id": payload.command_id}
    elif isinstance(payload, DispatchAttemptStarted):
        kind = _ATTEMPT_STARTED
        data = {
            "attempt_id": payload.attempt_id,
            "command_id": payload.command_id,
            "attempt_number": payload.attempt_number,
            "claim_token": payload.claim_token,
            "worker_id": payload.worker_id,
        }
    elif isinstance(payload, CommandOutcomeReceived):
        kind = _OUTCOME_RECEIVED
        data = {
            "command_id": payload.command_id,
            "attempt_id": payload.attempt_id,
            "outcome": _outcome_to_data(payload.outcome),
        }
    elif isinstance(payload, DecisionCancelled):
        kind = _DECISION_CANCELLED
        data = {"trigger_event_id": payload.trigger_event_id}
    elif isinstance(payload, StepContinuationCancelled):
        kind = _STEP_CONTINUATION_CANCELLED
        data = {"step_event_id": payload.step_event_id}
    elif isinstance(payload, DomainFactCommitted):
        kind = _DOMAIN_FACT_COMMITTED
        data = {
            "fact_type": payload.fact_type,
            "data": thaw_value(payload.data),
            "requests_decision": payload.requests_decision,
        }
    else:
        raise TypeError(type(payload).__name__)
    return kind, _json_dumps(data)


def decode_payload(
    kind: str,
    schema_version: int,
    payload_json: str,
) -> EventPayload:
    if schema_version != EVENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported event schema version: {schema_version}")
    raw = json.loads(payload_json)
    if not isinstance(raw, dict):
        raise ValueError(f"invalid event payload: {kind}")
    data: dict[str, object] = raw
    if kind == _USER_MESSAGE:
        return UserMessageReceived(data["content"])
    if kind == _STEP_COMMITTED:
        return StepCommitted(_step_from_data(data["step"]), data["decision_metadata"])
    if kind == _COMMAND_AUTHORIZED:
        return CommandAuthorized(data["command_id"])
    if kind == _COMMAND_REJECTED:
        return CommandRejected(data["command_id"])
    if kind == _ATTEMPT_STARTED:
        return DispatchAttemptStarted(**data)
    if kind == _OUTCOME_RECEIVED:
        return CommandOutcomeReceived(
            command_id=data["command_id"],
            attempt_id=data["attempt_id"],
            outcome=_outcome_from_data(data["outcome"]),
        )
    if kind == _DECISION_CANCELLED:
        return DecisionCancelled(data["trigger_event_id"])
    if kind == _STEP_CONTINUATION_CANCELLED:
        return StepContinuationCancelled(data["step_event_id"])
    if kind == _DOMAIN_FACT_COMMITTED:
        return DomainFactCommitted(
            fact_type=data["fact_type"],
            data=data["data"],
            requests_decision=data["requests_decision"],
        )
    raise ValueError(f"unsupported event type: {kind}")


def delivery_fingerprint(draft: EventDraft) -> str:
    kind, payload_json = encode_payload(draft.payload)
    value = {
        "session_id": draft.session_id,
        "event_type": kind,
        "schema_version": draft.schema_version,
        "payload": json.loads(payload_json),
        "causation_id": draft.causation_id,
        "correlation_id": draft.correlation_id,
        "artifact_refs": list(draft.artifact_refs),
    }
    digest = sha256(_json_dumps(value).encode("utf-8")).hexdigest()
    return f"v{DELIVERY_FINGERPRINT_VERSION}:{digest}"


def _attempt_to_data(attempt: AttemptState) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id,
        "attempt_number": attempt.attempt_number,
        "started_event_id": attempt.started_event_id,
        "phase": attempt.phase.value,
        "outcome": (
            _outcome_to_data(attempt.outcome) if attempt.outcome is not None else None
        ),
    }


def _attempt_from_data(data: dict[str, object]) -> AttemptState:
    return AttemptState(
        attempt_id=data["attempt_id"],
        attempt_number=data["attempt_number"],
        started_event_id=data["started_event_id"],
        phase=AttemptPhase(data["phase"]),
        outcome=(
            _outcome_from_data(data["outcome"]) if data["outcome"] is not None else None
        ),
    )


def _command_state_to_data(state: CommandState) -> dict[str, object]:
    return {
        "command": _command_to_data(state.command),
        "phase": state.phase.value,
        "issued_by_event_id": state.issued_by_event_id,
        "attempts": [_attempt_to_data(attempt) for attempt in state.attempts],
        "outcome": (
            _outcome_to_data(state.outcome) if state.outcome is not None else None
        ),
        "canonical_outcome_event_id": state.canonical_outcome_event_id,
        "dispatch_eligible_by_event_id": state.dispatch_eligible_by_event_id,
        "authorization_rejected_by_event_id": (
            state.authorization_rejected_by_event_id
        ),
    }


def _command_state_from_data(data: dict[str, object]) -> CommandState:
    return CommandState(
        command=_command_from_data(data["command"]),
        phase=CommandPhase(data["phase"]),
        issued_by_event_id=data["issued_by_event_id"],
        attempts=tuple(_attempt_from_data(attempt) for attempt in data["attempts"]),
        outcome=(
            _outcome_from_data(data["outcome"]) if data["outcome"] is not None else None
        ),
        canonical_outcome_event_id=data["canonical_outcome_event_id"],
        dispatch_eligible_by_event_id=data["dispatch_eligible_by_event_id"],
        authorization_rejected_by_event_id=data["authorization_rejected_by_event_id"],
    )


def encode_state(state: CanonicalState) -> str:
    return _json_dumps(
        {
            "codec_version": STATE_CODEC_VERSION,
            "projection_version": STATE_PROJECTION_VERSION,
            "state": {
                "session_id": state.session_id,
                "journal_position": state.journal_position,
                "decision_cursor": state.decision_cursor,
                "status": state.status.value,
                "commands": [
                    _command_state_to_data(command) for command in state.commands
                ],
                "steps": [_step_state_to_data(step) for step in state.steps],
                "next_trigger_event_id": state.next_trigger_event_id,
                "waiting_command_ids": list(state.waiting_command_ids),
                "waiting_for": list(state.waiting_for),
            },
        }
    )


def decode_state(state_json: str) -> CanonicalState:
    envelope = json.loads(state_json)
    if type(envelope) is not dict:
        raise TypeError("state checkpoint must be object")
    if envelope["codec_version"] != STATE_CODEC_VERSION:
        raise ValueError("unsupported state codec version")
    if envelope["projection_version"] != STATE_PROJECTION_VERSION:
        raise ValueError("unsupported state projection version")
    data = envelope["state"]
    if type(data) is not dict:
        raise TypeError("canonical state must be object")
    return CanonicalState(
        session_id=data["session_id"],
        journal_position=data["journal_position"],
        decision_cursor=data["decision_cursor"],
        status=RuntimeStatus(data["status"]),
        commands=tuple(
            _command_state_from_data(command) for command in data["commands"]
        ),
        steps=tuple(_step_state_from_data(step) for step in data["steps"]),
        next_trigger_event_id=data["next_trigger_event_id"],
        waiting_command_ids=tuple(data["waiting_command_ids"]),
        waiting_for=tuple(data["waiting_for"]),
    )
