"""会话与工作区的绑定。

工作区是沙箱边界：一条会话只属于一个工作区，落进去即固定。绑定是会话
创建时写进日志的一条领域事实，此后只读——没有第二处事实源。
"""

from __future__ import annotations

from redpanda.runtime import DomainFactCommitted, Event
from redpanda.sandbox.registry import WorkspaceRecord, WorkspaceRegistry

SESSION_WORKSPACE_FACT = "session.workspace"


class UnboundSessionError(ValueError):
    """会话没有工作区归属：存量会话按归档处理，不参与运行与展示。"""

    code = "unbound_session"

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"会话没有工作区归属：{session_id}")


def workspace_binding(workspace_id: str) -> DomainFactCommitted:
    if type(workspace_id) is not str or not workspace_id:
        raise ValueError("workspace_id must be a non-empty str")
    return DomainFactCommitted(
        SESSION_WORKSPACE_FACT,
        {"workspace_id": workspace_id},
        requests_decision=False,
    )


def bound_workspace_id(events: tuple[Event, ...]) -> str | None:
    for event in events:
        payload = event.payload
        if (
            isinstance(payload, DomainFactCommitted)
            and payload.fact_type == SESSION_WORKSPACE_FACT
        ):
            return payload.data["workspace_id"]
    return None


def bound_workspace(
    session_id: str,
    events: tuple[Event, ...],
    workspaces: WorkspaceRegistry,
) -> WorkspaceRecord:
    workspace_id = bound_workspace_id(events)
    if workspace_id is None:
        raise UnboundSessionError(session_id)
    return workspaces.get(workspace_id)
