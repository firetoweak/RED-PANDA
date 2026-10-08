from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from redpanda.runtime.json_values import thaw_value


Arguments: TypeAlias = tuple[tuple[str, object], ...]

# 授权判定钩子：签发 Command 时对工具参数求值，返回 True 表示需要用户确认。
# 必须是纯函数——只看参数，不读外部状态。静态布尔是它的常量特例。
AuthorizationPolicy: TypeAlias = Callable[[Mapping[str, object]], bool]
MAX_JSON_VALUE_BYTES = 128 * 1024
MAX_JSON_VALUE_DEPTH = 32


def _require_str(value: object, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be str")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _require_optional_str(value: object, name: str) -> None:
    if value is not None:
        _require_str(value, name)


def _require_nonnegative_int(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative int")


# 工具参数文本不是 JSON object 时，Command 仍要入账，但参数里只有这份拒绝。
# 它不是工具 schema 的字段：Dispatcher 不调用工具，授权也不看它。
ARGUMENT_REJECTION_KEY = "argument_rejection"
_ARGUMENT_REJECTION_FIELDS = frozenset({"raw", "code", "error", "hint"})


def rejected_tool_arguments(
    raw: str,
    *,
    code: str,
    error: str,
    hint: str,
) -> Arguments:
    """把无法成为工具参数的原文装进 InvokeTool。"""

    if any(type(item) is not str for item in (raw, code, error, hint)):
        raise TypeError("argument rejection fields must be str")
    if not code or not error or not hint:
        raise ValueError("argument rejection code, error, and hint must not be empty")
    return ((
        ARGUMENT_REJECTION_KEY,
        {"raw": raw, "code": code, "error": error, "hint": hint},
    ),)


def argument_rejection(effect: InvokeTool) -> dict[str, str] | None:
    """参数拒绝的原文与工具结果；普通参数返回 None。"""

    if len(effect.arguments) != 1:
        return None
    key, value = effect.arguments[0]
    if key != ARGUMENT_REJECTION_KEY:
        return None
    payload = thaw_value(value)
    if type(payload) is not dict or set(payload) != _ARGUMENT_REJECTION_FIELDS:
        return None
    if any(type(item) is not str for item in payload.values()):
        return None
    if not payload["code"] or not payload["error"] or not payload["hint"]:
        return None
    return payload


def _require_str_tuple(value: object, name: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{name} must be tuple")
    if any(type(item) is not str or not item for item in value):
        raise ValueError(f"{name} contains invalid values")


def freeze_value(value: object) -> object:
    budget = [MAX_JSON_VALUE_BYTES]
    return _freeze_value(value, 0, budget)


def _freeze_value(
    value: object,
    depth: int,
    budget: list[int],
) -> object:
    if depth > MAX_JSON_VALUE_DEPTH:
        raise ValueError("JSON value exceeds maximum depth")
    if value is None or type(value) in (bool, int, float, str):
        if type(value) is float and not isfinite(value):
            raise ValueError("JSON number must be finite")
        encoded = (
            value.encode("utf-8") if type(value) is str else str(value).encode("ascii")
        )
        budget[0] -= len(encoded) + 2
        if budget[0] < 0:
            raise ValueError("JSON value exceeds maximum size")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be str")
        budget[0] -= 2
        if budget[0] < 0:
            raise ValueError("JSON value exceeds maximum size")
        return MappingProxyType(
            {
                key: _freeze_mapping_item(key, item, depth, budget)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        budget[0] -= 2
        if budget[0] < 0:
            raise ValueError("JSON value exceeds maximum size")
        return tuple(_freeze_value(item, depth + 1, budget) for item in value)
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def _freeze_mapping_item(
    key: str,
    value: object,
    depth: int,
    budget: list[int],
) -> object:
    budget[0] -= len(key.encode("utf-8")) + 3
    if budget[0] < 0:
        raise ValueError("JSON value exceeds maximum size")
    return _freeze_value(value, depth + 1, budget)


@dataclass(frozen=True, slots=True)
class InvokeTool:
    name: str
    arguments: Arguments = ()

    def __post_init__(self) -> None:
        _require_str(self.name, "tool name")
        if type(self.arguments) is not tuple:
            raise TypeError("tool arguments must be tuple")
        if any(
            type(argument) is not tuple or len(argument) != 2
            for argument in self.arguments
        ):
            raise TypeError("tool arguments must contain key-value tuples")
        keys = [key for key, _ in self.arguments]
        if any(type(key) is not str for key in keys):
            raise TypeError("tool argument keys must be str")
        if len(keys) != len(set(keys)):
            raise ValueError("tool arguments contain duplicate keys")
        object.__setattr__(
            self,
            "arguments",
            tuple((key, freeze_value(value)) for key, value in self.arguments),
        )

    def argument_dict(self) -> dict[str, object]:
        """Return detached JSON arguments for execution and external encoding."""
        return {key: thaw_value(value) for key, value in self.arguments}


CommandEffect: TypeAlias = InvokeTool


@dataclass(frozen=True, slots=True)
class Command:
    """Committed side-effect.

    `requires_authorization` and `decision_on_outcome` are assembly
    information captured at issue time. They are not an approval policy
    and not inferred from the tool name. `requires_authorization` may
    come from a static spec flag or from evaluating an AuthorizationPolicy
    against the issued arguments; once captured it is a fixed fact.
    """

    command_id: str
    effect: CommandEffect
    requires_authorization: bool = False
    decision_on_outcome: bool = True

    def __post_init__(self) -> None:
        _require_str(self.command_id, "command id")
        if type(self.effect) is not InvokeTool:
            raise TypeError("command effect is invalid")
        if type(self.requires_authorization) is not bool:
            raise TypeError("requires_authorization must be bool")
        if type(self.decision_on_outcome) is not bool:
            raise TypeError("decision_on_outcome must be bool")


@dataclass(frozen=True, slots=True)
class ModelDecision:
    content: str = ""
    command_requests: tuple[CommandEffect, ...] = ()

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise TypeError("decision content must be str")
        if type(self.command_requests) is not tuple:
            raise TypeError("command requests must be tuple")
        if any(type(request) is not InvokeTool for request in self.command_requests):
            raise TypeError("command request is invalid")
        if not self.content.strip() and not self.command_requests:
            raise ValueError("decision must contain content or effects")


@dataclass(frozen=True, slots=True)
class Step:
    step_id: str
    trigger_event_id: str
    decision_cursor: int
    basis_state_version: str
    observed_journal_position: int
    decision: ModelDecision
    commands: tuple[Command, ...]

    def __post_init__(self) -> None:
        _require_str(self.step_id, "step id")
        _require_str(self.trigger_event_id, "trigger event id")
        _require_str(self.basis_state_version, "basis state version")
        if type(self.decision_cursor) is not int or self.decision_cursor < 1:
            raise ValueError("decision cursor must be positive")
        if (
            type(self.observed_journal_position) is not int
            or self.observed_journal_position < 1
        ):
            raise ValueError("observed journal position must be positive")
        if type(self.decision) is not ModelDecision:
            raise TypeError("step decision is invalid")
        if type(self.commands) is not tuple:
            raise TypeError("step commands must be tuple")
        if any(type(command) is not Command for command in self.commands):
            raise TypeError("step command is invalid")
        command_ids = [command.command_id for command in self.commands]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("step commands contain duplicate ids")
        if (
            tuple(command.effect for command in self.commands)
            != self.decision.command_requests
        ):
            raise ValueError("step commands do not match decision requests")


class OutcomeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    status: OutcomeStatus
    value: object | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not OutcomeStatus:
            raise TypeError("outcome status is invalid")
        _require_optional_str(self.error_type, "outcome error type")
        _require_optional_str(self.error_message, "outcome error message")
        object.__setattr__(self, "value", freeze_value(self.value))


class CommandPhase(str, Enum):
    """Execution lifecycle only.

    Authorization is an orthogonal dispatch gate. PENDING means the
    command is issued and not yet terminal; it does not mean Dispatcher
    may claim it.
    """

    PENDING = "pending"
    UNKNOWN = "unknown"
    TERMINAL = "terminal"


class AttemptPhase(str, Enum):
    UNKNOWN = "unknown"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class AttemptState:
    attempt_id: str
    attempt_number: int
    started_event_id: str
    phase: AttemptPhase = AttemptPhase.UNKNOWN
    outcome: CommandOutcome | None = None

    def __post_init__(self) -> None:
        _require_str(self.attempt_id, "attempt id")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt number must be positive")
        _require_str(self.started_event_id, "attempt started event id")
        if type(self.phase) is not AttemptPhase:
            raise TypeError("attempt phase is invalid")
        if self.outcome is not None and type(self.outcome) is not CommandOutcome:
            raise TypeError("attempt outcome is invalid")


@dataclass(frozen=True, slots=True)
class CommandState:
    command: Command
    phase: CommandPhase
    issued_by_event_id: str
    attempts: tuple[AttemptState, ...] = ()
    outcome: CommandOutcome | None = None
    canonical_outcome_event_id: str | None = None
    dispatch_eligible_by_event_id: str | None = None
    authorization_rejected_by_event_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.command) is not Command:
            raise TypeError("command state command is invalid")
        if type(self.phase) is not CommandPhase:
            raise TypeError("command state phase is invalid")
        _require_str(self.issued_by_event_id, "issued by event id")
        if type(self.attempts) is not tuple or any(
            type(attempt) is not AttemptState for attempt in self.attempts
        ):
            raise TypeError("command attempts are invalid")
        attempt_ids = tuple(attempt.attempt_id for attempt in self.attempts)
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("command attempts contain duplicate ids")
        if self.outcome is not None and type(self.outcome) is not CommandOutcome:
            raise TypeError("command outcome is invalid")
        _require_optional_str(
            self.canonical_outcome_event_id,
            "canonical outcome event id",
        )
        _require_optional_str(
            self.dispatch_eligible_by_event_id,
            "dispatch eligible event id",
        )
        _require_optional_str(
            self.authorization_rejected_by_event_id,
            "authorization rejected event id",
        )

@dataclass(frozen=True, slots=True)
class StepState:
    """一个回合在历史里的完整形态。

    payload 里的 Step 只记录决策的起点：它提交时命令刚发出，结果还是之后
    才会到的另一批事件。回合因此是一段跨越多个事件的跨度，完整形态只能由
    投影给出，payload 无论加多少字段都到不了。
    """

    step: Step
    committed_event_id: str
    sequence: int
    decision_metadata: object
    commands: tuple[CommandState, ...]

    def __post_init__(self) -> None:
        if type(self.step) is not Step:
            raise TypeError("step state step is invalid")
        _require_str(self.committed_event_id, "committed event id")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("step sequence must be positive")
        if type(self.commands) is not tuple or any(
            type(state) is not CommandState for state in self.commands
        ):
            raise TypeError("step state commands are invalid")
        if tuple(state.command.command_id for state in self.commands) != tuple(
            command.command_id for command in self.step.commands
        ):
            raise ValueError("step state commands do not match the step")


@dataclass(frozen=True, slots=True)
class DecisionState:
    session_id: str
    version: str
    user_messages: tuple[str, ...]
    commands: tuple[CommandState, ...]
    steps: tuple[StepState, ...]
    visible_event_ids: tuple[str, ...]
    consumed_trigger_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_str(self.session_id, "decision session id")
        _require_str(self.version, "decision state version")
        _require_str_tuple(self.user_messages, "decision user messages")
        if type(self.commands) is not tuple or any(
            type(command) is not CommandState for command in self.commands
        ):
            raise TypeError("decision commands are invalid")
        if type(self.steps) is not tuple or any(
            type(step) is not StepState for step in self.steps
        ):
            raise TypeError("decision steps are invalid")
        _require_str_tuple(self.visible_event_ids, "visible event ids")
        _require_str_tuple(
            self.consumed_trigger_event_ids,
            "consumed trigger event ids",
        )

    def command(self, command_id: str) -> CommandState:
        for state in self.commands:
            if state.command.command_id == command_id:
                return state
        raise KeyError(command_id)


class RuntimeStatus(str, Enum):
    RUNNABLE = "runnable"
    WAITING = "waiting"


@dataclass(frozen=True, slots=True)
class CanonicalState:
    session_id: str
    journal_position: int
    decision_cursor: int
    status: RuntimeStatus
    commands: tuple[CommandState, ...]
    steps: tuple[StepState, ...]
    next_trigger_event_id: str | None
    waiting_command_ids: tuple[str, ...]
    waiting_for: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_str(self.session_id, "canonical session id")
        _require_nonnegative_int(self.journal_position, "journal position")
        _require_nonnegative_int(self.decision_cursor, "decision cursor")
        if type(self.status) is not RuntimeStatus:
            raise TypeError("runtime status is invalid")
        if type(self.commands) is not tuple or any(
            type(command) is not CommandState for command in self.commands
        ):
            raise TypeError("canonical commands are invalid")
        if type(self.steps) is not tuple or any(
            type(step) is not StepState for step in self.steps
        ):
            raise TypeError("canonical steps are invalid")
        _require_optional_str(
            self.next_trigger_event_id,
            "next trigger event id",
        )
        _require_str_tuple(self.waiting_command_ids, "waiting command ids")
        _require_str_tuple(self.waiting_for, "waiting reasons")

    def command(self, command_id: str) -> CommandState:
        for state in self.commands:
            if state.command.command_id == command_id:
                return state
        raise KeyError(command_id)
