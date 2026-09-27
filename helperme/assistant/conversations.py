from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from helperme.assistant.control import pending_approval_view, project_control_message
from helperme.assistant.attachments import AttachmentGateway, AttachmentStore
from helperme.assistant.file_attachments import FileAttachment, is_file_attachment_id
from helperme.assistant.delivery import DELIVER_TOOL_NAME
from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.sessions import SessionView, session_view
from helperme.assistant.subagent.subagent import project_parent, project_pending
from helperme.assistant.workspaces import bound_workspace_id
from helperme.assistant.workspace_versions import WorkspaceVersionFact, project_workspace_versions
from helperme.runtime import (
    CommandPhase,
    CommandState,
    Event,
    OutcomeStatus,
    SqliteJournal,
    StepCommitted,
    StepState,
    UserMessageReceived,
    replay,
)


ToolStatus = Literal[
    "queued",
    "succeeded",
    "failed",
    "unknown",
    "awaiting_authorization",
    "rejected",
]
SessionActivity = Literal["running", "idle"]
UNKNOWN_TOOL_ERROR = "执行中断，结果未知"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    workspace_id: str
    title: str
    updated_at: datetime | None
    activity: SessionActivity


@dataclass(frozen=True, slots=True)
class UserItem:
    kind: Literal["user"]
    message_id: str
    text: str
    occurred_at: datetime
    images: tuple[str, ...] = ()
    files: tuple[FileAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolItem:
    command_id: str
    name: str
    status: ToolStatus
    error: str | None
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StepItem:
    kind: Literal["step"]
    step_id: str
    output_id: str
    text: str | None
    tools: tuple[ToolItem, ...]
    occurred_at: datetime
    thinking: str | None = None
    # 这一步跑完时的文件状态还记得住，人点它就能退回来。
    rewindable: bool = False


ConversationItem = UserItem | StepItem


@dataclass(frozen=True, slots=True)
class ConversationView:
    session_id: str
    workspace_id: str | None
    revision: int
    items: tuple[ConversationItem, ...]
    session: SessionView
    compact_count: int = 0
    compact_phase: str | None = None
    waiting_until: datetime | None = None
    workspace_version: WorkspaceVersionFact | None = None


class AssistantQueries:
    """Read-only Assistant projections for native Channels."""

    def __init__(self, store: SessionStore, sessions) -> None:
        self._store = store
        self._sessions = sessions

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        summaries: list[SessionSummary] = []
        for path in self._store.journals():
            journal = SqliteJournal(path)
            session_id = await journal.session_identity()
            events = await journal.snapshot(session_id)
            if project_parent(events) is not None:
                continue
            if not any(
                isinstance(event.payload, UserMessageReceived) for event in events
            ):
                continue
            workspace_id = bound_workspace_id(events)
            if workspace_id is None:
                continue
            if self._sessions.is_superseded(session_id):
                continue
            if self._sessions.is_archived(session_id):
                continue
            summaries.append(
                project_session_summary(
                    session_id,
                    events,
                    workspace_id=workspace_id,
                    activity=self._sessions.activity(session_id),
                    title=self._sessions.session_title(session_id),
                )
            )
        return tuple(
            sorted(
                summaries,
                key=lambda item: (
                    item.updated_at is not None,
                    item.updated_at or datetime.min,
                    item.session_id,
                ),
                reverse=True,
            )
        )

    async def conversation(
        self,
        session_id: str,
        *,
        view: SessionView | None = None,
    ) -> ConversationView:
        events = await SqliteJournal(
            self._store.require(session_id)
        ).snapshot(session_id)
        state = replay(session_id, events).state
        if view is None:
            view = session_view(
                state,
                has_active_subagents=bool(project_pending(events)),
                control_approval=pending_approval_view(events),
                control_message=project_control_message(events),
                auto_authorize=self._sessions.auto_authorize(session_id),
                paused=self._sessions.is_paused(session_id),
            )
        status = self._sessions.conversation_status(session_id)
        scheduled = self._sessions.next_scheduled_check(session_id)
        return replace(
            project_conversation(
                session_id,
                events,
                state.steps,
                session=view,
                attachments=AttachmentGateway(self._store.root).for_session(session_id),
            ),
            compact_count=status.compact_count,
            compact_phase=status.compact_phase,
            waiting_until=None if scheduled is None else scheduled.due_at,
        )


def recent_workspace_id(summaries: tuple[SessionSummary, ...]) -> str | None:
    """最近一次聊天的工作区；列表已按 updated_at 降序，取第一条即可。"""
    return summaries[0].workspace_id if summaries else None


def project_session_summary(
    session_id: str,
    events: tuple[Event, ...],
    *,
    workspace_id: str,
    activity: SessionActivity,
    title: str | None = None,
) -> SessionSummary:
    if title is None:
        title = "新会话"
        for event in events:
            if isinstance(event.payload, UserMessageReceived):
                title = event.payload.content.strip().splitlines()[0]
                break
    return SessionSummary(
        session_id=session_id,
        workspace_id=workspace_id,
        title=title,
        updated_at=events[-1].occurred_at if events else None,
        activity=activity,
    )


def project_conversation(
    session_id: str,
    events: tuple[Event, ...],
    steps: tuple[StepState, ...],
    *,
    session: SessionView,
    attachments: AttachmentStore | None = None,
) -> ConversationView:
    by_event = {step.committed_event_id: step for step in steps}
    versions = project_workspace_versions(events)
    rewindable = {fact.step_id for fact in versions if fact.version is not None}
    items: list[ConversationItem] = []
    for event in events:
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            file_ids = tuple(ref for ref in event.artifact_refs if is_file_attachment_id(ref))
            if file_ids and attachments is None:
                raise ValueError("文件引用必须装配 Session 附件存储")
            items.append(
                UserItem(
                    "user",
                    event.event_id,
                    payload.content,
                    event.occurred_at,
                    tuple(ref for ref in event.artifact_refs if not is_file_attachment_id(ref)),
                    tuple(attachments.files.describe(ref) for ref in file_ids),
                )
            )
            continue
        if not isinstance(payload, StepCommitted):
            continue
        step = by_event[event.event_id]
        text = step.step.decision.content.strip()
        thinking = _step_thinking(step.decision_metadata)
        tools: list[ToolItem] = []
        for command_state in step.commands:
            effect = command_state.command.effect
            if effect.name == DELIVER_TOOL_NAME:
                continue
            status, error = _tool_status(command_state)
            tools.append(
                ToolItem(
                    command_state.command.command_id,
                    effect.name,
                    status,
                    error,
                    effect.argument_dict(),
                )
            )
        if text or tools or thinking:
            items.append(
                StepItem(
                    "step",
                    payload.step.step_id,
                    payload.step.trigger_event_id,
                    text or None,
                    tuple(tools),
                    event.occurred_at,
                    thinking,
                    payload.step.step_id in rewindable,
                )
            )
    return ConversationView(
        session_id=session_id,
        workspace_id=bound_workspace_id(events),
        revision=events[-1].sequence if events else 0,
        items=tuple(items),
        session=session,
        workspace_version=versions[-1] if versions else None,
    )


def _step_thinking(metadata: object) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    extensions = metadata.get("message_extensions")
    if not isinstance(extensions, Mapping):
        return None
    text = extensions.get("reasoning_content")
    if type(text) is not str:
        return None
    stripped = text.strip()
    return stripped or None


def _indeterminate_error(value: object) -> str | None:
    """`ok is None` 是结果不确定的既有约定，不认具体工具的结果码。"""

    if not isinstance(value, Mapping) or "ok" not in value or value["ok"] is not None:
        return None
    error = value["error"]
    if type(error) is not str or not error:
        raise ValueError("结果不确定的 outcome 必须带 error")
    return error


def _tool_status(
    state: CommandState,
) -> tuple[ToolStatus, str | None]:
    """只从 Journal 投影工具基态；running 由 Channel 按 command_id 叠加。"""

    outcome = state.outcome
    if outcome is not None:
        if outcome.status is OutcomeStatus.SUCCEEDED:
            indeterminate = _indeterminate_error(outcome.value)
            if indeterminate is not None:
                return ("unknown", indeterminate)
            return ("succeeded", None)
        return ("failed", outcome.error_message)
    if state.authorization_rejected_by_event_id is not None:
        return ("rejected", None)
    if state.phase is CommandPhase.UNKNOWN:
        return ("unknown", UNKNOWN_TOOL_ERROR)
    if state.dispatch_eligible_by_event_id is None:
        return ("awaiting_authorization", None)
    return ("queued", None)
