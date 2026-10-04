"""工具管理控制面的审批协议，不是 Runtime Command 授权。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Protocol

if TYPE_CHECKING:
    from redpanda.tools.spec import ToolSpec


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _freeze(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ControlApprovalProposal:
    action: str
    payload: Mapping[str, Any]
    summary: str
    risk: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True, slots=True)
class ControlPreparationFailure:
    """准备边界已经识别、可以作为确定事实记录的外部失败。"""

    result: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", _freeze(self.result))


@dataclass(frozen=True, slots=True)
class ControlApprovalExecution:
    succeeded: bool
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", dict(self.data))


class ControlApprovalHandler(Protocol):
    action: str

    async def execute(
        self,
        payload: Mapping[str, Any],
    ) -> ControlApprovalExecution:
        ...


@dataclass(frozen=True, slots=True)
class ControlOperation:
    domain: str
    proposal_spec: ToolSpec
    approval_handler: ControlApprovalHandler

    def __post_init__(self) -> None:
        if type(self.domain) is not str or not self.domain:
            raise ValueError("control operation domain must be a non-empty str")
        if not self.proposal_spec.control_boundary:
            raise ValueError("对话控制工具必须声明 control_boundary")
        if not self.proposal_spec.exclusive_batch:
            raise ValueError("对话控制工具必须声明 exclusive_batch")
        if (
            type(self.approval_handler.action) is not str
            or not self.approval_handler.action
        ):
            raise ValueError("control approval action must be a non-empty str")

    @property
    def name(self) -> str:
        return self.proposal_spec.name

    @property
    def action(self) -> str:
        return self.approval_handler.action
