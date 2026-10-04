"""Assistant 对话中的 Host 控制面，不进入 Runtime Command。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from uuid import uuid4

from redpanda.runtime.events import (
    DomainFactCommitted,
    Event,
    StepCommitted,
    UserMessageReceived,
)
from redpanda.runtime.json_values import thaw_value
from redpanda.runtime.state import DecisionFrame
from redpanda.tools.control import (
    ControlApprovalExecution,
    ControlApprovalProposal,
    ControlOperation,
    ControlPreparationFailure,
)
from redpanda.tools.spec import ToolArgumentsError


CONTROL_SOURCE = "assistant.control"
CONTROL_REQUEST_METADATA = "control_request"

CONTROL_PROPOSED = "assistant.control.proposed"
CONTROL_CONCLUDED = "assistant.control.concluded"
CONTROL_PREPARATION_FAILED = "assistant.control.preparation_failed"
CONTROL_APPROVED = "assistant.control.approved"
CONTROL_REJECTED = "assistant.control.rejected"
CONTROL_EXECUTION_STARTED = "assistant.control.execution_started"
CONTROL_SUCCEEDED = "assistant.control.succeeded"
CONTROL_FAILED = "assistant.control.failed"

_CONTROL_FACTS = frozenset({
    CONTROL_PROPOSED,
    CONTROL_CONCLUDED,
    CONTROL_PREPARATION_FAILED,
    CONTROL_APPROVED,
    CONTROL_REJECTED,
    CONTROL_EXECUTION_STARTED,
    CONTROL_SUCCEEDED,
    CONTROL_FAILED,
})

_TERMINAL_PHASES = frozenset({
    "concluded",
    "preparation_failed",
    "rejected",
    "succeeded",
    "failed",
})


class ControlArgumentsError(ValueError):
    def __init__(self, details: object) -> None:
        super().__init__("control arguments validation failed")
        self.details = details


class NoPendingControlApproval(LookupError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"当前 Session 没有待确认的控制操作：{session_id}")


class ControlDecisionConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ControlIntent:
    request_id: str
    tool: str
    action: str
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class ControlApprovalRequest:
    id: str
    action: str
    payload: Mapping[str, object]
    summary: str
    risk: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class ControlApprovalView:
    request_id: str
    summary: str
    risk: str


@dataclass(frozen=True, slots=True)
class ControlOutcome:
    fact_type: str
    data: Mapping[str, object]
    delivery_id: str
    requests_decision: bool


@dataclass(frozen=True, slots=True)
class ControlState:
    intent: ControlIntent
    phase: str = "requested"
    request: ControlApprovalRequest | None = None
    execution_id: str | None = None
    message: str | None = None
    data: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ControlProjection:
    states: tuple[ControlState, ...]
    active: ControlState | None

    def get(self, request_id: str) -> ControlState | None:
        return next(
            (state for state in self.states if state.intent.request_id == request_id),
            None,
        )

    @property
    def message(self) -> str | None:
        if self.active is not None:
            if self.active.phase == "execution_started":
                return (
                    f"控制操作 `{self.active.intent.action}` 的执行结果未知"
                    f"（{self.active.execution_id}），不会自动重试。"
                )
            return None
        for state in reversed(self.states):
            if state.phase in {"rejected", "succeeded", "failed"}:
                return state.message
        return None


@dataclass(frozen=True, slots=True)
class _DecisionKey:
    session_id: str
    trigger_event_id: str
    decision_cursor: int
    basis_state_version: str


@dataclass(frozen=True, slots=True)
class _StagedCall:
    key: _DecisionKey
    intent: ControlIntent


def _require_object(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} 字段无效")
    return value


def _require_str(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} 必须是非空字符串")
    return value


def _intent_from_data(value: object) -> ControlIntent:
    data = _require_object(
        value,
        {"request_id", "tool", "action", "arguments"},
        "控制请求",
    )
    arguments = data["arguments"]
    if type(arguments) is not dict:
        raise ValueError("控制请求 arguments 必须是 object")
    return ControlIntent(
        _require_str(data["request_id"], "控制 request_id"),
        _require_str(data["tool"], "控制 tool"),
        _require_str(data["action"], "控制 action"),
        arguments,
    )


def _request_from_fact(value: object) -> ControlApprovalRequest:
    data = _require_object(
        value,
        {"request_id", "action", "payload", "summary", "risk"},
        "控制提案事实",
    )
    payload = data["payload"]
    if type(payload) is not dict:
        raise ValueError("控制提案事实 payload 必须是 object")
    return ControlApprovalRequest(
        _require_str(data["request_id"], "控制 request_id"),
        _require_str(data["action"], "控制 action"),
        payload,
        _require_str(data["summary"], "控制 summary"),
        _require_str(data["risk"], "控制 risk"),
    )


def _fact_data(payload: DomainFactCommitted) -> dict[str, object]:
    value = thaw_value(payload.data)
    if type(value) is not dict:
        raise ValueError(f"{payload.fact_type} data 必须是 object")
    return value


def _require_transition(state: ControlState, phase: str) -> None:
    allowed = {
        "requested": {"proposed", "concluded", "preparation_failed"},
        "proposed": {"approved", "rejected"},
        "approved": {"execution_started"},
        "execution_started": {"succeeded", "failed"},
    }
    if phase not in allowed.get(state.phase, set()):
        raise ValueError(
            f"控制状态迁移无效: {state.intent.request_id} "
            f"{state.phase} -> {phase}"
        )


def _apply_fact(state: ControlState, payload: DomainFactCommitted) -> ControlState:
    data = _fact_data(payload)
    fact_type = payload.fact_type
    expected_decision = fact_type in {
        CONTROL_CONCLUDED,
        CONTROL_PREPARATION_FAILED,
        CONTROL_REJECTED,
        CONTROL_SUCCEEDED,
        CONTROL_FAILED,
    }
    if payload.requests_decision is not expected_decision:
        raise ValueError(f"{fact_type} requests_decision 无效")

    if fact_type == CONTROL_PROPOSED:
        request = _request_from_fact(data)
        _require_transition(state, "proposed")
        if request.id != state.intent.request_id or request.action != state.intent.action:
            raise ValueError("控制提案 identity 与已提交请求不匹配")
        return replace(state, phase="proposed", request=request)

    phase_by_fact = {
        CONTROL_CONCLUDED: "concluded",
        CONTROL_PREPARATION_FAILED: "preparation_failed",
        CONTROL_APPROVED: "approved",
        CONTROL_REJECTED: "rejected",
        CONTROL_EXECUTION_STARTED: "execution_started",
        CONTROL_SUCCEEDED: "succeeded",
        CONTROL_FAILED: "failed",
    }
    phase = phase_by_fact[fact_type]
    _require_transition(state, phase)

    request_id = _require_str(data.get("request_id"), "控制 request_id")
    action = _require_str(data.get("action"), "控制 action")
    if request_id != state.intent.request_id or action != state.intent.action:
        raise ValueError("控制事实 identity 与已提交请求不匹配")

    if phase in {"concluded", "preparation_failed"}:
        _require_object(data, {"request_id", "tool", "action", "result"}, fact_type)
        if _require_str(data["tool"], "控制 tool") != state.intent.tool:
            raise ValueError("控制事实 tool 与已提交请求不匹配")
        result = data["result"]
        if type(result) is not dict:
            raise ValueError(f"{fact_type} result 必须是 object")
        return replace(state, phase=phase, data=result)

    if phase == "approved":
        _require_object(data, {"request_id", "action"}, fact_type)
        return replace(state, phase=phase)

    if phase == "rejected":
        _require_object(
            data,
            {"request_id", "action", "message", "data"},
            fact_type,
        )
        result_data = data["data"]
        if type(result_data) is not dict:
            raise ValueError(f"{fact_type} data.data 必须是 object")
        return replace(
            state,
            phase=phase,
            message=_require_str(data["message"], "控制 message"),
            data=result_data,
        )

    execution_id = _require_str(data.get("execution_id"), "控制 execution_id")
    if execution_id != execution_id_for(request_id):
        raise ValueError("控制 execution_id 不是由 request_id 确定性派生")
    if phase == "execution_started":
        _require_object(
            data,
            {"request_id", "action", "execution_id"},
            fact_type,
        )
        return replace(state, phase=phase, execution_id=execution_id)

    _require_object(
        data,
        {"request_id", "action", "execution_id", "message", "data"},
        fact_type,
    )
    result_data = data["data"]
    if type(result_data) is not dict:
        raise ValueError(f"{fact_type} data.data 必须是 object")
    return replace(
        state,
        phase=phase,
        execution_id=execution_id,
        message=_require_str(data["message"], "控制 message"),
        data=result_data,
    )


def project_control(events: Sequence[Event]) -> ControlProjection:
    states: list[ControlState] = []
    indexes: dict[str, int] = {}
    active_id: str | None = None

    for event in events:
        payload = event.payload
        if isinstance(payload, StepCommitted):
            metadata = thaw_value(payload.decision_metadata)
            if metadata is None:
                continue
            if type(metadata) is not dict:
                raise ValueError("Step decision_metadata 必须是 object")
            request_data = metadata.get(CONTROL_REQUEST_METADATA)
            if request_data is None:
                continue
            intent = _intent_from_data(request_data)
            if intent.request_id in indexes:
                raise ValueError(f"重复控制 request_id: {intent.request_id}")
            if active_id is not None:
                raise ValueError(
                    f"控制请求重叠: {active_id} / {intent.request_id}"
                )
            indexes[intent.request_id] = len(states)
            states.append(ControlState(intent))
            active_id = intent.request_id
            continue

        if not isinstance(payload, DomainFactCommitted):
            continue
        if (
            payload.fact_type.startswith(f"{CONTROL_SOURCE}.")
            and payload.fact_type not in _CONTROL_FACTS
        ):
            raise ValueError(f"未知控制事实: {payload.fact_type}")
        if payload.fact_type not in _CONTROL_FACTS:
            continue
        data = _fact_data(payload)
        request_id = _require_str(data.get("request_id"), "控制 request_id")
        index = indexes.get(request_id)
        if index is None:
            raise ValueError(f"控制事实没有对应请求: {request_id}")
        if active_id != request_id:
            raise ValueError(f"控制事实不属于当前活动请求: {request_id}")
        updated = _apply_fact(states[index], payload)
        states[index] = updated
        if updated.phase in _TERMINAL_PHASES:
            active_id = None

    active = None if active_id is None else states[indexes[active_id]]
    return ControlProjection(tuple(states), active)


def project_pending_approval(
    events: Sequence[Event],
) -> ControlApprovalRequest | None:
    active = project_control(events).active
    if active is None or active.phase != "proposed":
        return None
    assert active.request is not None
    return active.request


def pending_approval_view(events: Sequence[Event]) -> ControlApprovalView | None:
    request = project_pending_approval(events)
    if request is None:
        return None
    return ControlApprovalView(request.id, request.summary, request.risk)


def project_control_message(events: Sequence[Event]) -> str | None:
    """Human tip for the latest finished control result.

    The Journal facts stay. Once the user commits another message, the tip
    no longer belongs to the current turn. An execution that started and has
    no terminal fact keeps its unknown notice.
    """

    projection = project_control(events)
    if projection.active is not None:
        return projection.message
    message = projection.message
    if message is None:
        return None
    terminal_at = None
    for event in events:
        payload = event.payload
        if (
            isinstance(payload, DomainFactCommitted)
            and payload.fact_type
            in {CONTROL_REJECTED, CONTROL_SUCCEEDED, CONTROL_FAILED}
        ):
            terminal_at = event.sequence
    if terminal_at is None:
        return message
    for event in events:
        if (
            isinstance(event.payload, UserMessageReceived)
            and event.sequence > terminal_at
        ):
            return None
    return message


def execution_id_for(request_id: str) -> str:
    return f"control-execution-{request_id}"


class AssistantControlPlane:
    """从已提交 Step 恢复准备，并在持久执行边界之后调用 Application。"""

    def __init__(self, operations: Sequence[ControlOperation]) -> None:
        self._operations = {operation.name: operation for operation in operations}
        if len(self._operations) != len(operations):
            raise ValueError("对话控制工具名称重复")
        self._approval_operations = {
            operation.action: operation for operation in operations
        }
        if len(self._approval_operations) != len(operations):
            raise ValueError("控制审批 action 重复")
        self._staged: dict[str, _StagedCall] = {}
        self._active_sessions: set[str] = set()

    def schemas(
        self,
        session_id: str,
        events: Sequence[Event],
        allowed_names: frozenset[str] | None = None,
    ) -> list[dict[str, object]]:
        if (
            session_id in self._active_sessions
            or project_control(events).active is not None
        ):
            return []
        names = self.names() if allowed_names is None else allowed_names
        unknown = names.difference(self._operations)
        if unknown:
            raise ValueError(f"未知控制工具: {sorted(unknown)}")
        return [
            operation.proposal_spec.to_openai_tool()
            for name, operation in self._operations.items()
            if name in names
        ]

    def names(self) -> frozenset[str]:
        return frozenset(self._operations)

    def begin_decision(self, session_id: str) -> None:
        self._staged.pop(session_id, None)

    def stage(
        self,
        frame: DecisionFrame,
        name: str,
        arguments: Mapping[str, object],
    ) -> None:
        operation = self._operations[name]
        try:
            operation.proposal_spec.parameters.validate(dict(arguments))
        except ToolArgumentsError as exc:
            raise ControlArgumentsError(exc.details) from exc
        key = _DecisionKey(
            frame.state.session_id,
            frame.trigger_event.event_id,
            frame.decision_cursor,
            frame.basis_state_version,
        )
        self._staged[frame.state.session_id] = _StagedCall(
            key,
            ControlIntent(
                f"control-request-{uuid4().hex}",
                name,
                operation.action,
                dict(arguments),
            ),
        )

    def take_staged_metadata(self, frame: DecisionFrame) -> dict[str, object] | None:
        staged = self._staged.pop(frame.state.session_id, None)
        if staged is None:
            return None
        expected = _DecisionKey(
            frame.state.session_id,
            frame.trigger_event.event_id,
            frame.decision_cursor,
            frame.basis_state_version,
        )
        if staged.key != expected:
            raise RuntimeError("控制请求暂存与当前决策不匹配")
        intent = staged.intent
        return {
            "request_id": intent.request_id,
            "tool": intent.tool,
            "action": intent.action,
            "arguments": dict(intent.arguments),
        }

    async def prepare_pending(
        self,
        events: Sequence[Event],
    ) -> ControlOutcome | None:
        state = project_control(events).active
        if state is None or state.phase != "requested":
            return None
        intent = state.intent
        operation = self._operations[intent.tool]
        if operation.action != intent.action:
            raise ValueError("控制请求 tool/action 不匹配")
        try:
            input_data = operation.proposal_spec.parameters.validate(
                dict(intent.arguments)
            )
        except ToolArgumentsError as exc:
            raise ValueError("已提交控制请求不符合其参数契约") from exc

        session_id = events[0].session_id
        self._active_sessions.add(session_id)
        try:
            result = await operation.proposal_spec.handler(input_data)
        finally:
            self._active_sessions.remove(session_id)

        if isinstance(result, ControlApprovalProposal):
            if result.action != intent.action:
                raise ValueError("控制 proposal action 与已提交请求不匹配")
            return ControlOutcome(
                CONTROL_PROPOSED,
                {
                    "request_id": intent.request_id,
                    "action": result.action,
                    "payload": dict(result.payload),
                    "summary": result.summary,
                    "risk": result.risk,
                },
                f"{intent.request_id}:proposed",
                False,
            )
        if isinstance(result, ControlPreparationFailure):
            return ControlOutcome(
                CONTROL_PREPARATION_FAILED,
                {
                    "request_id": intent.request_id,
                    "tool": intent.tool,
                    "action": intent.action,
                    "result": dict(result.result),
                },
                f"{intent.request_id}:preparation_failed",
                True,
            )
        if type(result) is not dict:
            raise TypeError("控制准备返回值不符合契约")
        return ControlOutcome(
            CONTROL_CONCLUDED,
            {
                "request_id": intent.request_id,
                "tool": intent.tool,
                "action": intent.action,
                "result": result,
            },
            f"{intent.request_id}:concluded",
            True,
        )

    @staticmethod
    def decision_outcome(
        request: ControlApprovalRequest,
        *,
        approved: bool,
    ) -> ControlOutcome:
        if approved:
            return ControlOutcome(
                CONTROL_APPROVED,
                {"request_id": request.id, "action": request.action},
                f"{request.id}:approved",
                False,
            )
        message = f"已取消控制操作：{request.action}"
        return ControlOutcome(
            CONTROL_REJECTED,
            {
                "request_id": request.id,
                "action": request.action,
                "message": message,
                "data": {},
            },
            f"{request.id}:rejected",
            True,
        )

    @staticmethod
    def execution_started_outcome(
        request: ControlApprovalRequest,
    ) -> ControlOutcome:
        return ControlOutcome(
            CONTROL_EXECUTION_STARTED,
            {
                "request_id": request.id,
                "action": request.action,
                "execution_id": execution_id_for(request.id),
            },
            f"{request.id}:execution_started",
            False,
        )

    async def execute(
        self,
        request: ControlApprovalRequest,
    ) -> ControlApprovalExecution:
        operation = self._approval_operations[request.action]
        return await operation.approval_handler.execute(request.payload)

    @staticmethod
    def terminal_outcome(
        request: ControlApprovalRequest,
        execution: ControlApprovalExecution,
    ) -> ControlOutcome:
        phase = "succeeded" if execution.succeeded else "failed"
        return ControlOutcome(
            CONTROL_SUCCEEDED if execution.succeeded else CONTROL_FAILED,
            {
                "request_id": request.id,
                "action": request.action,
                "execution_id": execution_id_for(request.id),
                "message": execution.message,
                "data": dict(execution.data),
            },
            f"{request.id}:{phase}",
            True,
        )
