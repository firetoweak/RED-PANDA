"""Session file roots: a Bot's own directory or a TUI's registered workspace."""

from __future__ import annotations

from redpanda.runtime import DomainFactCommitted, Event
from redpanda.filesystem.registry import WorkspaceRecord, WorkspaceRegistry
from redpanda.filesystem.workspace import FileWorkspace
from pathlib import Path

SESSION_WORKSPACE_FACT = "session.workspace"
SESSION_DIRECTORY_FACT = "session.directory"


def directory_binding(directory: Path) -> DomainFactCommitted:
    return DomainFactCommitted(SESSION_DIRECTORY_FACT, {"directory": str(directory.resolve())}, requests_decision=False)


def bound_directory(events: tuple[Event, ...]) -> Path | None:
    for event in events:
        payload = event.payload
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == SESSION_DIRECTORY_FACT:
            if set(payload.data) != {"directory"} or payload.requests_decision:
                raise ValueError("invalid session directory fact")
            value = payload.data["directory"]
            if type(value) is not str or not Path(value).is_absolute():
                raise ValueError("session directory must be an absolute path")
            return Path(value)
    return None


class UnboundSessionError(ValueError):
    """A running Session has no file root."""

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
) -> WorkspaceRecord | FileWorkspace:
    directory = bound_directory(events)
    if directory is not None:
        return FileWorkspace(directory)
    workspace_id = bound_workspace_id(events)
    if workspace_id is None:
        raise UnboundSessionError(session_id)
    return workspaces.get(workspace_id)
