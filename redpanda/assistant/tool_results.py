from __future__ import annotations

from redpanda.tools.control import ControlApprovalProposal, ControlPreparationFailure


def runtime_tool_result(result: object) -> object:
    """Translate a product tool result without interpreting its domain value."""

    if isinstance(result, (ControlApprovalProposal, ControlPreparationFailure)):
        return {
            "ok": False,
            "code": "HOST_CONTROL_PLANE",
            "error": "这个操作需要用户确认，不能直接执行。",
            "hint": "改用对应的 propose_* 管理工具提交，由用户确认后执行。",
        }
    return result
