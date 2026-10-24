from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from konden.assistant.control import pending_approval_view, project_control_message
from konden.assistant.external_messages import EXTERNAL_MESSAGE, Feedback, parse_external
from konden.assistant.agents import COMMUNICATION_LIMIT_REACHED, PRIVATE_MESSAGE, parse_agent_fact, project_agent
from konden.assistant.compact.core import latest_input_tokens
from konden.assistant.attachments import AttachmentGateway, AttachmentStore
from konden.assistant.file_attachments import FileAttachment, is_file_attachment_id
from konden.assistant.agent_messages import SEND_FILE, SEND_MESSAGE
from konden.assistant.delivery import DELIVER_TOOL_NAME
from konden.runtime.json_values import thaw_value
from konden.assistant.work_plan import WorkPlanUpdate, project_work_plan, project_work_plan_updates
from konden.assistant.host.session_store import SessionStore
from konden.assistant.sessions import SessionView, session_view
from konden.assistant.subagent.subagent import (
    RETURN_FACT, UnreportedChildReturn, project_delegate_intents,
    project_pending, project_unreported_returns,
)
from konden.assistant.runner import SessionNotFoundError, pending_authorization_ids
from konden.assistant.workspaces import bound_workspace_id
from konden.runtime import (
    CommandPhase,
    CommandState,
    DomainFactCommitted,
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
AgentStatus = Literal["working", "authorization", "waiting", "idle"]
UNKNOWN_TOOL_ERROR = "执行已开始，但未取得最终结果，原因未知，可能已部分执行"


@dataclass(frozen=True, slots=True)
class AgentProjectView:
    name: str
    workspace_id: str
    directory: str


@dataclass(frozen=True, slots=True)
class AgentRosterEntry:
    session_id: str
    projects: tuple[AgentProjectView, ...]
    name: str
    activity: SessionActivity
    status: AgentStatus


@dataclass(frozen=True, slots=True)
class AuthorizationInboxItem:
    kind: Literal["authorization"]
    session_id: str
    workspace_id: str | None
    label: str
    command_id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class ControlInboxItem:
    kind: Literal["control"]
    session_id: str
    workspace_id: str | None
    label: str
    request_id: str
    summary: str
    risk: str


@dataclass(frozen=True, slots=True)
class LimitInboxItem:
    """A hop or origin limit the person has not answered in this session yet."""

    kind: Literal["limit"]
    session_id: str
    workspace_id: str | None
    label: str
    event_id: str
    recipient: str
    text: str


@dataclass(frozen=True, slots=True)
class AbsenceInboxItem:
    kind: Literal["absence"]
    session_id: str
    workspace_id: str | None
    label: str
    phase: Literal["asked", "paused"]
    summary: str


InboxItem = AuthorizationInboxItem | ControlInboxItem | LimitInboxItem | AbsenceInboxItem


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
    files: tuple[FileAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class PrivateMessageItem:
    kind: Literal["private_message"]
    message_id: str
    direction: Literal["sent", "received"]
    peer: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ExternalMessageItem:
    kind: Literal["external_message"]
    message_id: str
    direction: Literal["sent", "received"]
    peer: str
    identity: str
    text: str
    revision: str
    reference: str
    previous_request_id: str | None
    feedback: Feedback | None
    occurred_at: datetime


ConversationItem = UserItem | StepItem | PrivateMessageItem | ExternalMessageItem


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
    work_plan: dict | None = None
    work_plan_updates: tuple[WorkPlanUpdate, ...] = ()
    context_input_tokens: int | None = None
    subagent_terminals: tuple[UnreportedChildReturn, ...] = ()
    deleted: bool = False
    agent_name: str | None = None


@dataclass(frozen=True, slots=True)
class SubagentResult:
    failure: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class SubagentObservation:
    session_id: str
    task: str
    activity: SessionActivity
    result: SubagentResult | None
    conversation: ConversationView | None


class AssistantQueries:
    """Read-only Assistant projections for native Channels."""

    def __init__(self, store: SessionStore, sessions) -> None:
        self._store = store
        self._sessions = sessions

    async def observe_subagent(self, parent_session_id: str, command_id: str) -> SubagentObservation:
        events = await SqliteJournal(self._store.require(parent_session_id)).snapshot(parent_session_id)
        intent = next((item for item in project_delegate_intents(events) if item.command_id == command_id), None)
        if intent is None:
            raise SessionNotFoundError(f"委派不存在：{command_id}")
        child_id = intent.child_session_id
        # 委派已提交，但 Host 尚未物化子 Journal 时，仍可打开任务说明。
        if not self._store.path(child_id).parent.exists():
            return SubagentObservation(child_id, intent.task, "idle", None, None)
        child_events = await SqliteJournal(self._store.require(child_id)).snapshot(child_id)
        returned = next((event.payload.data for event in child_events
                         if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == RETURN_FACT), None)
        result = None if returned is None else SubagentResult(
            failure=returned["failure"], reason=returned["reason"],
        )
        return SubagentObservation(
            child_id, intent.task, self._sessions.activity(child_id), result,
            await self._conversation(child_id, child_events),
        )

    async def list_agents(self) -> tuple[AgentRosterEntry, ...]:
        entries: list[AgentRosterEntry] = []
        for record in await self._sessions.agents.list():
            events = await SqliteJournal(self._store.require(record.session_id)).snapshot(
                record.session_id
            )
            entries.append(project_agent_roster_entry(
                record.session_id,
                events,
                name=record.name,
                activity=self._sessions.activity(record.session_id),
                last_error=self._sessions.current_failure(record.session_id),
                auto_authorize=self._sessions.auto_authorize(),
            ))
        return tuple(entries)

    async def agent_entry(self, session_id: str) -> AgentRosterEntry:
        events = await SqliteJournal(self._store.require(session_id)).snapshot(session_id)
        agent = project_agent(events)
        if agent is None:
            raise SessionNotFoundError(session_id)
        return project_agent_roster_entry(
            session_id,
            events,
            name=agent.name,
            activity=self._sessions.activity(session_id),
            last_error=self._sessions.current_failure(session_id),
            auto_authorize=self._sessions.auto_authorize(),
        )

    async def list_inbox(self) -> tuple[InboxItem, ...]:
        items: list[InboxItem] = []
        watches = {item.session_id: item for item in self._sessions.absence_watches()}
        for path in self._store.journals():
            journal = SqliteJournal(path)
            session_id = await journal.session_identity()
            events = await journal.snapshot(session_id)
            agent = project_agent(events)
            if agent is None or agent.deleted:
                continue
            state = replay(session_id, events).state
            view = session_view(state, control_approval=pending_approval_view(events),
                                auto_authorize=self._sessions.auto_authorize())
            if view.pending_authorization_commands or view.control_approval is not None:
                items.extend(project_inbox_items(
                    session_id=session_id, workspace_id=None, label=agent.name,
                    authorizations=view.pending_authorization_commands,
                    control=view.control_approval,
                ))
            items.extend(project_limit_items(
                session_id=session_id, workspace_id=None, label=agent.name, events=events,
            ))
            watch = watches.get(session_id)
            if watch is not None:
                summary = (
                    "你离开后，未读汇报和例行任务触发都到了门槛。要让例行任务继续吗？"
                    if watch.phase == "asked" else
                    "无人查看，已暂停当时仍在运行的例行任务。恢复只恢复这些。"
                )
                items.append(AbsenceInboxItem(
                    "absence", session_id, None, agent.name, watch.phase, summary,
                ))
        return tuple(sorted(items, key=_inbox_sort_key))

    async def delivery_output_ids(
        self, session_id: str, source: str, delivery_ids: tuple[str, ...],
    ) -> dict[str, str]:
        """Journal event id for each delivery. A step started by that event uses the same id."""
        if not delivery_ids:
            return {}
        events = await SqliteJournal(self._store.require(session_id)).snapshot(session_id)
        wanted = set(delivery_ids)
        found: dict[str, str] = {}
        for event in events:
            delivery = event.delivery
            if delivery is None or delivery.source != source or delivery.delivery_id not in wanted:
                continue
            found[delivery.delivery_id] = event.event_id
        return found

    async def conversation(
        self,
        session_id: str,
        *,
        view: SessionView | None = None,
    ) -> ConversationView:
        events = await SqliteJournal(
            self._store.require(session_id)
        ).snapshot(session_id)
        return await self._conversation(session_id, events, view=view)

    async def _conversation(self, session_id: str, events: tuple[Event, ...], *, view: SessionView | None = None) -> ConversationView:
        state = replay(session_id, events).state
        terminals = project_unreported_returns(events, await self._child_returns(events))
        still_open = bool(
            set(project_pending(events)) - {item.child_session_id for item in terminals}
        )
        if view is None:
            view = session_view(
                state,
                has_open_subagents=still_open,
                control_approval=pending_approval_view(events),
                control_message=project_control_message(events),
                auto_authorize=self._sessions.auto_authorize(),
                last_error=self._sessions.current_failure(session_id),
            )
        else:
            view = replace(view, has_open_subagents=still_open,
                           last_error=self._sessions.current_failure(session_id))
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
            subagent_terminals=terminals,
        )

    async def _child_returns(self, events: tuple[Event, ...]) -> dict[str, Mapping[str, object]]:
        """待回收的子会话里已经写下的回传。没有 Journal 或还没有回传的跳过。"""

        found: dict[str, Mapping[str, object]] = {}
        for child_id in project_pending(events):
            if not self._store.path(child_id).parent.exists():
                continue
            child_events = await SqliteJournal(self._store.require(child_id)).snapshot(child_id)
            returned = next(
                (
                    event.payload.data
                    for event in child_events
                    if isinstance(event.payload, DomainFactCommitted)
                    and event.payload.fact_type == RETURN_FACT
                ),
                None,
            )
            if returned is not None:
                found[child_id] = returned
        return found


def project_agent_status(
    *,
    activity: SessionActivity,
    pending_authorization: bool,
    control_approval: bool,
    waiting_for: tuple[str, ...],
    last_error: str | None,
) -> AgentStatus:
    """名册状态只归约 Worker、授权和等待这些已经存在的事实。"""
    if activity == "running":
        return "working"
    if pending_authorization or control_approval:
        return "authorization"
    if last_error is not None or waiting_for == ("external_fact",):
        return "waiting"
    return "idle"


def project_agent_roster_entry(
    session_id: str,
    events: tuple[Event, ...],
    *,
    name: str,
    activity: SessionActivity,
    last_error: str | None,
    auto_authorize: bool,
) -> AgentRosterEntry:
    state = replay(session_id, events).state
    agent = project_agent(events)
    return AgentRosterEntry(
        session_id=session_id,
        projects=tuple(AgentProjectView(item.name, item.workspace_id, item.worktree) for item in agent.projects),
        name=name,
        activity=activity,
        status=project_agent_status(
            activity=activity,
            pending_authorization=not auto_authorize and bool(pending_authorization_ids(state)),
            control_approval=pending_approval_view(events) is not None,
            waiting_for=state.waiting_for,
            last_error=last_error,
        ),
    )


def project_inbox_items(
    *,
    session_id: str,
    workspace_id: str | None,
    label: str,
    authorizations,
    control,
) -> tuple[InboxItem, ...]:
    """收件箱只搬出已经待人决定的命令授权和控制提案。"""
    items: list[InboxItem] = [
        AuthorizationInboxItem(
            "authorization", session_id, workspace_id, label,
            item.command_id, item.name, dict(item.arguments),
        )
        for item in authorizations
    ]
    if control is not None:
        items.append(ControlInboxItem(
            "control", session_id, workspace_id, label,
            control.request_id, control.summary, control.risk,
        ))
    return tuple(items)


def project_limit_items(*, session_id: str, workspace_id: str | None, label: str, events) -> tuple[LimitInboxItem, ...]:
    """A later user message means the person has taken up the limit report."""
    last_user = max(
        (event.sequence for event in events if isinstance(event.payload, UserMessageReceived)),
        default=-1,
    )
    items = []
    for event in events:
        payload = event.payload
        if (not isinstance(payload, DomainFactCommitted)
                or payload.fact_type != COMMUNICATION_LIMIT_REACHED
                or event.sequence < last_user):
            continue
        notice = parse_agent_fact(payload)
        items.append(LimitInboxItem(
            "limit", session_id, workspace_id, label, event.event_id, notice.recipient, notice.text,
        ))
    return tuple(items)


def _inbox_sort_key(item: InboxItem) -> tuple:
    if item.kind == "authorization":
        identity = item.command_id
    elif item.kind == "control":
        identity = item.request_id
    elif item.kind == "limit":
        identity = item.event_id
    else:
        identity = item.session_id
    return (item.label, item.session_id, item.kind, identity)


def _delivered_files(command_state, attachments: AttachmentStore | None) -> tuple[FileAttachment, ...]:
    if attachments is None:
        raise ValueError("文件交付必须装配 Session 附件存储")
    attachment_id = thaw_value(command_state.outcome.value)["data"]["attachment"]["attachment_id"]
    return (attachments.files.describe(attachment_id),)


def project_conversation(
    session_id: str,
    events: tuple[Event, ...],
    steps: tuple[StepState, ...],
    *,
    session: SessionView,
    attachments: AttachmentStore | None = None,
) -> ConversationView:
    by_event = {step.committed_event_id: step for step in steps}
    sent_messages = {}
    for step in steps:
        for command in step.commands:
            outcome = command.outcome
            if (command.command.effect.name == SEND_MESSAGE and outcome is not None
                    and outcome.status is OutcomeStatus.SUCCEEDED):
                result = thaw_value(outcome.value)
                if result["ok"] is True and result["code"] == "DELIVERED":
                    sent_messages[command.canonical_outcome_event_id] = result["data"]
    communication_notices: dict[str, list] = {}
    for event in events:
        if (isinstance(event.payload, DomainFactCommitted)
                and event.payload.fact_type == COMMUNICATION_LIMIT_REACHED):
            communication_notices.setdefault(event.causation_id, []).append(
                parse_agent_fact(event.payload))
    items: list[ConversationItem] = []
    external_peers: dict[str, str] = {}
    for event in events:
        payload = event.payload
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == EXTERNAL_MESSAGE:
            message = parse_external(payload)
            peer = message.name
            if message.incoming:
                external_peers[message.message_id] = message.name
            else:
                peer = external_peers[message.reply_to]
            items.append(ExternalMessageItem("external_message", message.message_id,
                "received" if message.incoming else "sent", peer, message.identity, message.text,
                message.revision, message.reference, message.previous_request_id, message.feedback, event.occurred_at))
            continue
        if event.event_id in sent_messages:
            message = sent_messages[event.event_id]
            items.append(PrivateMessageItem("private_message", message["message_id"],
                                           "sent", message["recipient"], event.occurred_at))
            continue
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == PRIVATE_MESSAGE:
            message = parse_agent_fact(payload)
            if message.sender.kind == "agent":
                items.append(PrivateMessageItem("private_message", message.message_id,
                                               "received", message.sender.name, event.occurred_at))
            continue
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
        for notice in communication_notices.get(event.event_id, ()):
            text = "\n\n".join(filter(None, (text,
                f"与 {notice.recipient} 的交流已达上限，需要你判断是否继续。")))
        thinking = _step_thinking(step.decision_metadata)
        tools: list[ToolItem] = []
        messages: list[StepItem] = []
        for command_state in step.commands:
            effect = command_state.command.effect
            if effect.name == DELIVER_TOOL_NAME:
                continue
            if effect.name == SEND_FILE:
                if not _file_sent(command_state):
                    # A rejected or unfinished call reached nobody; it stays a tool record.
                    status, error = _tool_status(command_state, auto_authorize=session.auto_authorize)
                    tools.append(ToolItem(command_state.command.command_id, effect.name, status, error,
                                          effect.argument_dict()))
                    continue
                messages.append(StepItem(
                    "step", f"{payload.step.step_id}:{command_state.command.command_id}",
                    command_state.command.command_id, effect.argument_dict()["text"], (),
                    event.occurred_at, files=_delivered_files(command_state, attachments)))
                continue
            status, error = _tool_status(command_state, auto_authorize=session.auto_authorize)
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
                )
            )
        items.extend(messages)
    agent = project_agent(events)
    return ConversationView(
        session_id=session_id,
        workspace_id=bound_workspace_id(events),
        revision=events[-1].sequence if events else 0,
        items=tuple(items),
        session=session,
        work_plan=project_work_plan(events),
        work_plan_updates=project_work_plan_updates(events),
        context_input_tokens=latest_input_tokens(events),
        deleted=agent is not None and agent.deleted,
        agent_name=None if agent is None else agent.name,
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


def _file_sent(state: CommandState) -> bool:
    outcome = state.outcome
    return (outcome is not None and outcome.status is OutcomeStatus.SUCCEEDED
            and thaw_value(outcome.value).get("code") == "FILE_SENT")


def _tool_status(
    state: CommandState,
    *,
    auto_authorize: bool,
) -> tuple[ToolStatus, str | None]:
    """自动放行中的调用展示为排队；running 由 Channel 按 command_id 叠加。"""

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
    if state.dispatch_eligible_by_event_id is None and not auto_authorize:
        return ("awaiting_authorization", None)
    return ("queued", None)
