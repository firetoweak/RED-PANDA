from __future__ import annotations

from helperme.tools.control import ControlApprovalProposal, ControlPreparationFailure


def runtime_tool_result(result: object) -> object:
    """Translate a product tool result without interpreting its domain value."""

    if isinstance(result, (ControlApprovalProposal, ControlPreparationFailure)):
        action = result.action if isinstance(result, ControlApprovalProposal) else "preparation"
        return {
            "ok": False,
            "code": "HOST_CONTROL_PLANE",
            "error": f"这个操作需要用户确认，不能作为普通工具调用执行: {action}",
            "hint": "改用对应的 propose_* 管理工具提交，由用户确认后执行。",
        }
    return result
