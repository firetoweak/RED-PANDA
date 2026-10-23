"""Bot identities and communication facts; no routing or scheduling."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from redpanda.assistant.workspaces import bound_directory
from redpanda.runtime import (
    AttemptOrphaned, CommandOutcomeReceived, CommandRejected, DomainFactCommitted, replay,
)
from redpanda.runtime.json_values import thaw_value


AGENT_CREATED = "bot.agent_created"
AGENT_RENAMED = "bot.agent_renamed"
AGENT_DELETED = "bot.agent_deleted"
PROJECT_LINKED = "bot.project_linked"
PROJECT_UNLINKED = "bot.project_unlinked"
PRIVATE_MESSAGE = "bot.private_message"
GROUP_MESSAGE = "bot.group_message"
GROUP_ATTENTION = "bot.group_attention"
GROUP_CREATED = "bot.group_created"
GROUP_DISSOLVED = "bot.group_dissolved"
DIRECTION_DECLARED = "bot.direction_declared"
DIRECTION_SET_BY_USER = "bot.direction_set_by_user"
COLLEAGUE_JOINED = "bot.colleague_joined"
COMMUNICATION_LIMIT_REACHED = "bot.communication_limit_reached"
ROUTINES_UNWATCHED = "bot.routines_unwatched"
SOURCE = "coding_bot"

Text = Annotated[str, Field(min_length=1)]


class _Fact(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    @field_validator("*", check_fields=False)
    @classmethod
    def nonblank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("文字不能为空白")
        return value


class AgentCreated(_Fact):
    name: Text
    directory: Text

    @field_validator("name")
    @classmethod
    def stable_name(cls, value):
        if value != value.strip():
            raise ValueError("Agent 名字不能以空白开头或结尾")
        return value

    @field_validator("directory")
    @classmethod
    def absolute_directory(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("Agent directory 必须是绝对路径")
        return value


class AgentRenamed(_Fact):
    session_id: Text
    old_name: Text
    name: Text

    @field_validator("old_name", "name")
    @classmethod
    def stable_name(cls, value):
        return AgentCreated.stable_name(value)


class AgentDeleted(_Fact):
    session_id: Text
    name: Text


class GroupDissolved(_Fact):
    group: Text


class ProjectLinked(_Fact):
    """A registered project mounted into this Bot: its own worktree on its own branch.

    `name` is how the Bot addresses the project; it is unique among its mounts.
    """
    workspace_id: Text
    name: Text
    worktree: Text
    branch: Text

    @field_validator("worktree")
    @classmethod
    def absolute_worktree(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("Agent worktree 必须是绝对路径")
        return value


class ProjectUnlinked(_Fact):
    workspace_id: Text
    name: Text


class Speaker(_Fact):
    kind: Literal["user", "agent"]
    name: Text | None

    @model_validator(mode="after")
    def identity(self):
        if (self.kind == "user") != (self.name is None):
            raise ValueError("人的发言不带 Agent 名字；Agent 发言必须有名字")
        return self


class _Message(_Fact):
    message_id: Text
    sender: Speaker
    reply_to: Text | None
    text: Text


class PrivateMessage(_Message):
    """Origin names the non-Agent fact that ultimately drove this message."""
    recipient: Text
    thread_id: Text
    hop: Annotated[int, Field(ge=0)]
    origin: Text


class GroupMessage(_Message):
    group: Text
    mentioned_agents: list[Text]
    origin: Text | None

    @model_validator(mode="after")
    def human_messages_are_origins(self):
        if (self.sender.kind == "user") != (self.origin is None):
            raise ValueError("人的群发言本身就是源头；Agent 群发言必须带源头")
        return self


class GroupAttention(_Fact):
    group: Text
    message_id: Text


class GroupCreated(_Fact):
    group: Text
    members: Annotated[list[Text], Field(min_length=2, max_length=6)]

    @field_validator("members")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("群成员不能重复")
        return value


class DirectionDeclared(_Fact):
    name: Text
    direction: Text
    source: ClassVar[Literal["user", "agent"]] = "agent"


class DirectionSetByUser(DirectionDeclared):
    source: ClassVar[Literal["user", "agent"]] = "user"


class ColleagueJoined(_Fact):
    name: Text


class CommunicationLimitReached(_Fact):
    recipient: Text
    text: Text


class RoutinesUnwatched(_Fact):
    """The person was away, so the Host paused or restored this Agent's routines."""

    action: Literal["paused", "resumed"]
    routine_ids: list[str]
    text: Text


_FACTS = {
    AGENT_CREATED: (AgentCreated, True),
    AGENT_RENAMED: (AgentRenamed, False),
    AGENT_DELETED: (AgentDeleted, False),
    PROJECT_LINKED: (ProjectLinked, False),
    PROJECT_UNLINKED: (ProjectUnlinked, False),
    PRIVATE_MESSAGE: (PrivateMessage, True),
    GROUP_MESSAGE: (GroupMessage, False),
    GROUP_ATTENTION: (GroupAttention, True),
    GROUP_CREATED: (GroupCreated, False),
    GROUP_DISSOLVED: (GroupDissolved, False),
    DIRECTION_DECLARED: (DirectionDeclared, False),
    DIRECTION_SET_BY_USER: (DirectionSetByUser, False),
    COLLEAGUE_JOINED: (ColleagueJoined, False),
    COMMUNICATION_LIMIT_REACHED: (CommunicationLimitReached, False),
    ROUTINES_UNWATCHED: (RoutinesUnwatched, False),
}
AGENT_FACT_TYPES = frozenset(_FACTS)


def fact_arguments(fact: _Fact, *, delivery_id: str) -> dict:
    kind, requests_decision = next(
        (kind, requests_decision)
        for kind, (model, requests_decision) in _FACTS.items()
        if type(fact) is model
    )
    return dict(fact_type=kind, data=fact.model_dump(), source=SOURCE,
                delivery_id=delivery_id, requests_decision=requests_decision)


def parse_agent_fact(payload: DomainFactCommitted) -> _Fact:
    model, requests_decision = _FACTS[payload.fact_type]
    if payload.requests_decision is not requests_decision:
        raise ValueError(f"invalid decision trigger for {payload.fact_type}")
    return model.model_validate(thaw_value(payload.data))


@dataclass(frozen=True, slots=True)
class AgentRecord:
    session_id: str
    identity: AgentCreated
    projects: tuple[ProjectLinked, ...] = ()
    renamed_names: tuple[str, ...] = ()
    deleted: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        return (self.identity.name, *self.renamed_names)

    def renamed(self, name: str) -> AgentRecord:
        return replace(self, renamed_names=(*self.renamed_names, name))

    @property
    def name(self) -> str:
        return self.renamed_names[-1] if self.renamed_names else self.identity.name

    @property
    def directory(self) -> Path:
        return Path(self.identity.directory)

    def project(self, name: str) -> ProjectLinked | None:
        return next((item for item in self.projects if item.name == name), None)

    def project_by_id(self, workspace_id: str) -> ProjectLinked | None:
        return next((item for item in self.projects if item.workspace_id == workspace_id), None)


def project_agent(events) -> AgentRecord | None:
    creation = None
    projects: dict[str, ProjectLinked] = {}
    for event in events:
        payload = event.payload
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == AGENT_CREATED:
            if creation is not None:
                raise ValueError("Agent has more than one creation fact")
            creation = AgentRecord(event.session_id, parse_agent_fact(payload))
        elif isinstance(payload, DomainFactCommitted) and payload.fact_type == AGENT_RENAMED:
            renamed = parse_agent_fact(payload)
            if renamed.session_id == event.session_id:
                if creation is None or creation.name != renamed.old_name:
                    raise ValueError("Agent rename differs from its current identity")
                creation = creation.renamed(renamed.name)
        elif isinstance(payload, DomainFactCommitted) and payload.fact_type == AGENT_DELETED:
            deleted = parse_agent_fact(payload)
            if deleted.session_id == event.session_id:
                if creation is None or creation.name != deleted.name:
                    raise ValueError("Agent deletion differs from its current identity")
                creation = replace(creation, deleted=True)
        elif isinstance(payload, DomainFactCommitted) and payload.fact_type == PROJECT_LINKED:
            linked = parse_agent_fact(payload)
            if linked.workspace_id in projects or any(item.name == linked.name for item in projects.values()):
                raise ValueError("Agent links one project, or one name, only once at a time")
            projects[linked.workspace_id] = linked
        elif isinstance(payload, DomainFactCommitted) and payload.fact_type == PROJECT_UNLINKED:
            unlinked = parse_agent_fact(payload)
            if projects.pop(unlinked.workspace_id, None) is None:
                raise ValueError("Agent unlinks a project it has not linked")
    if creation is not None:
        if creation.directory != bound_directory(events):
            raise ValueError("Agent directory differs from Session directory binding")
        creation = replace(creation, projects=tuple(projects.values()))
    return creation


def project_direction(events, agent: AgentRecord) -> str | None:
    """Colleagues' declarations also reach this timeline; only the Agent's own counts."""
    direction = None
    name = agent.names[0]
    for event in events:
        payload = event.payload
        if isinstance(payload, DomainFactCommitted) and payload.fact_type == AGENT_RENAMED:
            renamed = parse_agent_fact(payload)
            if renamed.session_id == agent.session_id:
                name = renamed.name
        if isinstance(payload, DomainFactCommitted) and payload.fact_type in {DIRECTION_DECLARED, DIRECTION_SET_BY_USER}:
            declaration = parse_agent_fact(payload)
            if declaration.name == name:
                direction = declaration.direction
    return direction


def identity_for_model(events) -> dict | None:
    """Who the Agent is, restated where earlier facts no longer reach the model."""
    agent = project_agent(events)
    if agent is None:
        return None
    shown = {"name": agent.name}
    direction = project_direction(events, agent)
    if direction is not None:
        shown["direction"] = direction
    groups = {}
    renames = []
    deleted_names = set()
    for event in events:
        if not isinstance(event.payload, DomainFactCommitted):
            continue
        if event.payload.fact_type == GROUP_CREATED:
            fact = parse_agent_fact(event.payload)
            groups[fact.group] = {"group": fact.group, "members": list(fact.members)}
        elif event.payload.fact_type == GROUP_DISSOLVED:
            groups.pop(parse_agent_fact(event.payload).group, None)
        elif event.payload.fact_type == AGENT_DELETED:
            fact = parse_agent_fact(event.payload)
            deleted_names.add(fact.name)
        elif event.payload.fact_type == AGENT_RENAMED:
            renames.append(parse_agent_fact(event.payload))
    for fact in renames:
        for group in groups.values():
            group["members"] = [fact.name if member == fact.old_name else member for member in group["members"]]
    for group in groups.values():
        group["members"] = [member for member in group["members"] if member not in deleted_names]
    shown["groups"] = list(groups.values())
    shown["projects"] = [{"project": item.name, "directory": item.worktree} for item in agent.projects]
    shown["notice"] = (
        "Your current agent profile: name, direction, group memberships, and linked projects.")
    return shown


PROJECT_LINKED_NOTICE = (
    "This project is linked to you. directory is your read/write project working directory. "
    "Use paths under it and set cwd to it for commands. "
    "Use project to name it in land_branch, sync_branch, and delegate.")


def agent_fact_for_model(payload: DomainFactCommitted) -> dict:
    """Only observable identities, messages and requested actions reach the model."""
    fact = parse_agent_fact(payload)
    if isinstance(fact, AgentRenamed):
        return {"old_name": fact.old_name, "name": fact.name,
                "notice": "This agent's name changed. Use the current name for messages and mentions; existing conversations and group memberships continue."}
    if isinstance(fact, AgentDeleted):
        return {"name": fact.name, "notice": (
            "The user deleted this agent. It no longer participates in collaboration "
            "and cannot receive private messages or group mentions.")}
    if isinstance(fact, GroupDissolved):
        return {"group": fact.group, "notice": (
            "The user dissolved this group. You can no longer send messages to it; "
            "existing chat history is retained.")}
    if isinstance(fact, AgentCreated):
        return {"name": fact.name, "notice": (
            f'Your agent name is "{fact.name}". If the user asks for your name, answer with "{fact.name}".')}
    if isinstance(fact, ProjectLinked):
        return {"project": fact.name, "directory": fact.worktree, "notice": PROJECT_LINKED_NOTICE}
    if isinstance(fact, ProjectUnlinked):
        return {"project": fact.name, "notice": "This project has been unlinked and its working directory removed."}
    if isinstance(fact, ColleagueJoined):
        return {"colleague": fact.name}
    if isinstance(fact, DirectionDeclared):
        return {"agent": fact.name, "direction": fact.direction, "source": fact.source,
                "notice": "The user updated this agent's direction." if fact.source == "user"
                else "This agent updated its direction."}
    if isinstance(fact, GroupAttention):
        return {"group": fact.group, "message_id": fact.message_id, "notice": (
            "It's your turn in this group chat. Reply with a single send_group_message if you have something worth adding, "
            "using reply_to to reference this message; if you have nothing new, end without a message. "
            "Never reveal private one-on-one context.")}
    if isinstance(fact, GroupCreated):
        return {"group": fact.group, "members": fact.members, "notice": (
            "You are a participant in this group chat. Use send_group_message to say something the room can see.")}
    if isinstance(fact, CommunicationLimitReached):
        return {"recipient": fact.recipient, "text": fact.text}
    if isinstance(fact, RoutinesUnwatched):
        return {"action": fact.action, "routine_ids": fact.routine_ids, "text": fact.text}
    shown = {"message_id": fact.message_id, "sender": fact.sender.model_dump(),
             "reply_to": fact.reply_to, "text": fact.text}
    if isinstance(fact, PrivateMessage):
        shown["recipient"] = fact.recipient
        shown["notice"] = "A private message just arrived. Reply to the sender with send_message."
    else:
        shown.update(group=fact.group, mentioned_agents=fact.mentioned_agents)
        shown["notice"] = "A message in this group chat. Use send_group_message to say something the room can see."
    if fact.sender.kind == "agent":
        shown["notice"] += " This is another assistant reaching out, not the user typing here. If it is just an FYI with nothing for you to do, it is fine to stay silent."
    return shown


def message_origin(events, command_id: str) -> str:
    """源头计数沿本批首条输入回溯，不据此判断行动的语义归属。"""
    session_id = events[0].session_id
    state = replay(session_id, events).state
    issued = {item.command.command_id: item.issued_by_event_id for item in state.commands}
    triggers = {item.committed_event_id: item.step.trigger_event_id for item in state.steps}
    by_id = {event.event_id: event for event in events}
    trigger = by_id[triggers[issued[command_id]]]
    while isinstance(trigger.payload, (CommandOutcomeReceived, CommandRejected, AttemptOrphaned)):
        trigger = by_id[triggers[issued[trigger.payload.command_id]]]
    payload = trigger.payload
    if isinstance(payload, DomainFactCommitted) and payload.fact_type == PRIVATE_MESSAGE:
        return parse_agent_fact(payload).origin
    if isinstance(payload, DomainFactCommitted) and payload.fact_type == GROUP_ATTENTION:
        attention = parse_agent_fact(payload)
        message = next(fact for event in events
                       if isinstance(event.payload, DomainFactCommitted)
                       and event.payload.fact_type == GROUP_MESSAGE
                       and (fact := parse_agent_fact(event.payload)).message_id == attention.message_id)
        return message.message_id if message.origin is None else message.origin
    return f"{trigger.session_id}/{trigger.event_id}"
