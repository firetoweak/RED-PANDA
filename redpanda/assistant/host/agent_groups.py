"""Host-owned group records and deterministic turn scheduling for group chats."""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable
from dataclasses import dataclass, field
from uuid import uuid4

from redpanda.assistant.agent_messages import (
    MAX_GROUP_REPLIES, MAX_GROUP_ROUNDS,
)
from redpanda.assistant.agents import (
    GROUP_CREATED, GROUP_MESSAGE, GroupAttention, GroupCreated, GroupMessage, Speaker,
    fact_arguments, parse_agent_fact,
)
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.workspaces import bound_workspace_id
from redpanda.runtime import AgentRuntime, DomainFactCommitted, SqliteJournal


class GroupNameTaken(ValueError):
    pass


class GroupNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class GroupRecord:
    group_id: str
    workspace_id: str
    created: GroupCreated

    @property
    def name(self) -> str:
        return self.created.group

    @property
    def members(self) -> list[str]:
        return self.created.members


def _targets(group: GroupRecord, speaker: str | None, mentioned: list[str]) -> list[str]:
    return [name for name in (mentioned or group.members) if name != speaker]


@dataclass(frozen=True, slots=True)
class _Call:
    """A member asked to answer a message, in a given round of the discussion that message belongs to."""

    message_id: str
    opener: str
    round: int


@dataclass(eq=False)
class _Drive:
    """Who the Host calls in one group, until a person speaks there again or stops it.

    A speech that quotes a message its sender was called to answer continues that
    call's discussion. Any other speech opens a discussion of its own, with its own
    round and reply budget, and joins the queue instead of replacing it.
    """

    group: GroupRecord
    origins: set[str] = field(default_factory=set)
    replies: Counter = field(default_factory=Counter)
    pending: dict[str, _Call] = field(default_factory=dict)
    # Calls not yet answered, by member and the message it was asked to answer.
    answering: dict[tuple[str, str], _Call] = field(default_factory=dict)
    waiting: dict[str, asyncio.Event] = field(default_factory=dict)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    running: bool = False

    def queue(self, speaker: str | None, mentioned: list[str], call: _Call) -> bool:
        """Queue whoever this speech calls; False when its discussion has no round left."""
        if call.round > MAX_GROUP_ROUNDS:
            return False
        for name in _targets(self.group, speaker, mentioned):
            self.pending.setdefault(name, call)
        return True


class AgentGroups:
    def __init__(self, host):
        self.host = host
        self.store = SessionStore(host.home.state_root / "groups")
        self._creation_lock = asyncio.Lock()
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._drives: dict[str, _Drive] = {}
        # A person's stop silences what those discussions go on to say, not unrelated later work.
        self._stopped_origins: dict[str, set[str]] = {}

    def _journal(self, group_id):
        return SqliteJournal(self.store.require(group_id))

    async def list(self, workspace_id: str | None = None) -> tuple[GroupRecord, ...]:
        groups = []
        for path in self.store.journals():
            journal = SqliteJournal(path)
            group_id = await journal.session_identity()
            events = await journal.snapshot(group_id)
            created, = (parse_agent_fact(event.payload) for event in events
                        if isinstance(event.payload, DomainFactCommitted)
                        and event.payload.fact_type == GROUP_CREATED)
            record = GroupRecord(group_id, bound_workspace_id(events), created)
            if workspace_id is None or record.workspace_id == workspace_id:
                groups.append(record)
        return tuple(sorted(groups, key=lambda group: (group.workspace_id, group.name)))

    async def get(self, group_id: str) -> GroupRecord:
        for group in await self.list():
            if group.group_id == group_id:
                return group
        raise GroupNotFound(group_id)

    async def view(self, group_id: str) -> dict:
        """What a person sees: the public record and who the Host is calling now."""
        group = await self.get(group_id)
        events = await self._journal(group_id).snapshot(group_id)
        drive = self._drives.get(group_id)
        active = drive is not None and drive.running and not drive.stopped.is_set()
        return {
            "group_id": group.group_id, "workspace_id": group.workspace_id, "name": group.name,
            "members": group.members, "calling": list(drive.waiting) if active else [],
            "active": active,
            "messages": [
                {"message_id": fact.message_id, "sender": fact.sender.name, "text": fact.text,
                 "mentioned_agents": fact.mentioned_agents, "reply_to": fact.reply_to,
                 "occurred_at": event.occurred_at.isoformat()}
                for event in events
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == GROUP_MESSAGE
                for fact in (parse_agent_fact(event.payload),)
            ],
        }

    async def messages(self, group_id: str) -> list[GroupMessage]:
        return [parse_agent_fact(event.payload) for event in await self._journal(group_id).snapshot(group_id)
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == GROUP_MESSAGE]

    async def create(self, *, workspace_id: str, name: str, members: list[str]) -> GroupRecord:
        self.host.workspaces.get(workspace_id)
        created = GroupCreated(group=name, members=members)
        agents = [await self.host.agents.resolve(workspace_id, member) for member in created.members]
        group_id = f"group-{uuid4().hex}"
        async with self._creation_lock:
            if any(group.name == name for group in await self.list(workspace_id)):
                raise GroupNameTaken(f"项目内已有群：{name}")
            await self.store.create(group_id, workspace_id=workspace_id,
                                    initial_fact=fact_arguments(created, delivery_id="created"))
        await asyncio.gather(*(
            self.host.request("fact", agent.session_id, fact_arguments(created, delivery_id=f"joined:{group_id}"))
            for agent in agents))
        # Other pages list groups too; a new group is a change they must hear about.
        self._changed(group_id)
        return GroupRecord(group_id, workspace_id, created)

    async def _publish(self, group: GroupRecord, message: GroupMessage) -> None:
        """The group record is the source; member timelines receive copies without deciding."""
        arguments = fact_arguments(message, delivery_id=message.message_id)
        await AgentRuntime(self._journal(group.group_id), None, {}).receive_domain_fact(group.group_id, **arguments)
        recipients = [await self.host.agents.resolve(group.workspace_id, name)
                      for name in group.members if name != message.sender.name]
        await asyncio.gather(*(self.host.request("fact", agent.session_id, arguments) for agent in recipients))
        self._changed(group.group_id)

    def _changed(self, group_id: str) -> None:
        """Tell observers the record or who is being called changed; never delays scheduling."""
        if self.host.group_changed_sink is not None:
            self.host._track(group_id, asyncio.create_task(self._notify(group_id)))

    async def _notify(self, group_id: str) -> None:
        observed = self.host.group_changed_sink(group_id)
        if isinstance(observed, Awaitable):
            await observed

    async def post(self, group_id: str, text: str, mention: list[str]) -> GroupMessage:
        """A human message starts a new drive, superseding the previous one."""
        group = await self.get(group_id)
        if any(name not in group.members for name in mention):
            raise ValueError("点名的成员不在这个群里")
        message = GroupMessage(message_id=f"group-message-{uuid4().hex}", sender=Speaker(kind="user", name=None),
                               reply_to=None, text=text, group=group.name, mentioned_agents=mention, origin=None)
        async with self._group_locks.setdefault(group_id, asyncio.Lock()):
            await self._publish(group, message)
            drive = self._replace_drive(group)
            drive.origins.add(message.message_id)
            drive.queue(None, mention, _Call(message.message_id, message.message_id, 1))
            self._schedule(drive)
        return message

    def _replace_drive(self, group: GroupRecord) -> _Drive:
        previous = self._drives.get(group.group_id)
        if previous is not None:
            previous.stopped.set()
        drive = _Drive(group)
        self._drives[group.group_id] = drive
        return drive

    def _schedule(self, drive: _Drive) -> None:
        if drive.pending and not drive.stopped.is_set() and not drive.running:
            drive.running = True
            self.host._track(drive.group.group_id, asyncio.create_task(self._run(drive)))

    async def _run(self, drive: _Drive) -> None:
        try:
            while drive.pending and not drive.stopped.is_set():
                calls, drive.pending = drive.pending, {}
                for name, call in calls.items():
                    if drive.stopped.is_set() or self.host.closed:
                        return
                    await self._call(drive, name, call)
        finally:
            # Cleared before any await so a speech arriving now starts a new run.
            drive.running = False
            self._changed(drive.group.group_id)

    async def _call(self, drive: _Drive, name: str, call: _Call) -> None:
        """A turn ends when the member speaks or its triggered work settles."""
        agent = await self.host.agents.resolve(drive.group.workspace_id, name)
        if self.host.is_paused(agent.session_id):
            return
        drive.answering[name, call.message_id] = call
        attention = fact_arguments(GroupAttention(group=drive.group.name, message_id=call.message_id),
                                   delivery_id=f"attention:{drive.group.group_id}:{call.message_id}:{name}")
        if self.host.activity(agent.session_id) == "running":
            # A member busy with other work answers once it gets to the call; nobody waits behind it.
            await self.host.request("fact", agent.session_id, attention)
            return
        spoke = drive.waiting[name] = asyncio.Event()
        self._changed(drive.group.group_id)
        try:
            await self.host.request("fact", agent.session_id, attention)
            waits = [asyncio.create_task(self.host.wait_quiescent(agent.session_id)),
                     asyncio.create_task(spoke.wait()), asyncio.create_task(drive.stopped.wait())]
            done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            del drive.waiting[name]
            self._changed(drive.group.group_id)

    async def send(self, session_id: str, arguments: dict, origin: str, admit):
        """An Agent speech. ``admit`` checks the per-origin budget once the send is known to be new.

        The speech answers a call only when its ``reply_to`` quotes the message the
        sender was called to answer and that call is still open; each call is
        answered once. Which message a speech answers is the model's statement and
        is never inferred from what started its work.
        """
        sender = await self.host.agents.by_session(session_id)
        group = next((group for group in await self.list(sender.workspace_id)
                      if group.name == arguments["group"]), None)
        if group is None:
            return {"ok": False, "code": "GROUP_NOT_FOUND", "error": "同项目中没有这个群。"}
        if sender.name not in group.members:
            return {"ok": False, "code": "NOT_A_MEMBER", "error": "你不在这个群里。"}
        mention = arguments["mention"]
        if any(name not in group.members or name == sender.name for name in mention):
            return {"ok": False, "code": "INVALID_MENTION", "error": "只能点名这个群里的其他成员。"}
        async with self._group_locks.setdefault(group.group_id, asyncio.Lock()):
            messages = await self.messages(group.group_id)
            identity = arguments["message_id"]
            existing = next((message for message in messages if message.message_id == identity), None)
            if existing is not None:
                if (existing.sender != Speaker(kind="agent", name=sender.name) or existing.group != group.name
                        or existing.text != arguments["text"] or existing.mentioned_agents != mention
                        or existing.reply_to != arguments["reply_to"]):
                    raise ValueError("persisted group message conflicts with its sending Command")
                await self._publish(group, existing)
                drive = self._drives.get(group.group_id)
                return self._delivered(group, identity, drive is not None and not drive.stopped.is_set(),
                                       origin in self._stopped_origins.get(group.group_id, ()))
            if arguments["reply_to"] is not None and all(
                    message.message_id != arguments["reply_to"] for message in messages):
                return {"ok": False, "code": "INVALID_REPLY", "error": "引用的消息必须是这个群里的消息。"}
            refused = await admit(group.name)
            if refused is not None:
                return refused
            drive = self._drives.get(group.group_id)
            if drive is None or drive.stopped.is_set():
                drive = self._replace_drive(group)
            answered = drive.answering.get((sender.name, arguments["reply_to"]))
            if answered is not None and drive.replies[answered.opener] >= MAX_GROUP_REPLIES:
                return {"ok": False, "code": "GROUP_LIMIT_REACHED",
                        "error": "这次群讨论的回复已达上限；需要某位同事接着处理时可以私信他。"}
            message = GroupMessage(message_id=identity, sender=Speaker(kind="agent", name=sender.name),
                                   reply_to=arguments["reply_to"], text=arguments["text"], group=group.name,
                                   mentioned_agents=mention, origin=origin)
            await self._publish(group, message)
            if answered is not None:
                del drive.answering[sender.name, arguments["reply_to"]]
            # Any speech from a member being called ends the Host's wait for it.
            if sender.name in drive.waiting:
                drive.waiting[sender.name].set()
            if origin in self._stopped_origins.get(group.group_id, ()):
                return self._delivered(group, identity, False, True)
            drive.origins.add(origin)
            if answered is None:
                calls = drive.queue(sender.name, mention, _Call(identity, identity, 1))
            else:
                drive.replies[answered.opener] += 1
                calls = drive.queue(sender.name, mention, _Call(identity, answered.opener, answered.round + 1))
            self._schedule(drive)
            return self._delivered(group, identity, calls)

    def _delivered(self, group, identity, calls, stopped=False):
        if calls:
            notice = "已送达；被叫到的成员会依次回应，不等待回复。"
        elif stopped:
            notice = "已送达；这次群讨论已被人停止，不会再叫成员回应。"
        else:
            notice = "已送达；这次群讨论已达轮数上限，不会再叫成员回应；需要某位同事接着处理时可以私信他。"
        return {"ok": True, "code": "DELIVERED", "data": {"message_id": identity, "group": group.name,
                "notice": notice}}

    async def stop(self, group_id: str) -> None:
        """Stop scheduling and interrupt members currently called; completed work stays."""
        drive = self._drives.get(group_id)
        if drive is None:
            return
        # Read who is being called before stopping releases their waits.
        called = list(drive.waiting)
        drive.stopped.set()
        self._stopped_origins.setdefault(group_id, set()).update(drive.origins)
        sessions = [(await self.host.agents.resolve(drive.group.workspace_id, name)).session_id for name in called]
        await asyncio.gather(*(self.host.interrupt_for_group(session_id) for session_id in sessions))
