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
from redpanda.runtime import AgentRuntime, DecisionCancelled, DomainFactCommitted, SqliteJournal, replay


class GroupNameTaken(ValueError):
    pass


class GroupNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class GroupRecord:
    group_id: str
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
    """Who the Host calls in one group, until a person speaks there again.

    A speech that quotes a message its sender was called to answer continues that
    call's discussion. Any other speech opens a discussion of its own, with its own
    round and reply budget, and joins the queue instead of replacing it.
    """

    group: GroupRecord
    replies: Counter = field(default_factory=Counter)
    pending: dict[str, _Call] = field(default_factory=dict)
    # Calls not yet answered, by member and the message it was asked to answer.
    answering: dict[tuple[str, str], _Call] = field(default_factory=dict)
    waiting: dict[str, asyncio.Event] = field(default_factory=dict)
    # Set when a person's new message replaces this drive.
    replaced: asyncio.Event = field(default_factory=asyncio.Event)
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

    def _journal(self, group_id):
        return SqliteJournal(self.store.require(group_id))

    async def list(self) -> tuple[GroupRecord, ...]:
        groups = []
        for path in self.store.journals():
            journal = SqliteJournal(path)
            group_id = await journal.session_identity()
            events = await journal.snapshot(group_id)
            created, = (parse_agent_fact(event.payload) for event in events
                        if isinstance(event.payload, DomainFactCommitted)
                        and event.payload.fact_type == GROUP_CREATED)
            record = GroupRecord(group_id, created)
            groups.append(record)
        return tuple(sorted(groups, key=lambda group: group.name))

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
        return {
            "group_id": group.group_id, "name": group.name,
            "members": group.members, "calling": list(drive.waiting) if drive is not None and drive.running else [],
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

    async def create(self, *, name: str, members: list[str]) -> GroupRecord:
        created = GroupCreated(group=name, members=members)
        agents = [await self.host.agents.resolve(member) for member in created.members]
        group_id = f"group-{uuid4().hex}"
        async with self._creation_lock:
            if any(group.name == name for group in await self.list()):
                raise GroupNameTaken(f"已有群：{name}")
            await self.store.create(group_id,
                                    initial_fact=fact_arguments(created, delivery_id="created"))
        await asyncio.gather(*(
            self.host.request("fact", agent.session_id, fact_arguments(created, delivery_id=f"joined:{group_id}"))
            for agent in agents))
        # Other pages list groups too; a new group is a change they must hear about.
        self._changed(group_id)
        return GroupRecord(group_id, created)

    async def _publish(self, group: GroupRecord, message: GroupMessage) -> None:
        """The group record is the source; member timelines receive copies without deciding."""
        arguments = fact_arguments(message, delivery_id=message.message_id)
        await AgentRuntime(self._journal(group.group_id), None, {}).receive_domain_fact(group.group_id, **arguments)
        recipients = [await self.host.agents.resolve(name)
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
            drive.queue(None, mention, _Call(message.message_id, message.message_id, 1))
            self._schedule(drive)
        return message

    def _replace_drive(self, group: GroupRecord) -> _Drive:
        previous = self._drives.get(group.group_id)
        if previous is not None:
            previous.replaced.set()
        drive = _Drive(group)
        self._drives[group.group_id] = drive
        return drive

    def _schedule(self, drive: _Drive) -> None:
        if drive.pending and not drive.replaced.is_set() and not drive.running:
            drive.running = True
            self.host._track(drive.group.group_id, asyncio.create_task(self._run(drive)))

    async def _run(self, drive: _Drive) -> None:
        try:
            while drive.pending and not drive.replaced.is_set():
                calls, drive.pending = drive.pending, {}
                for name, call in calls.items():
                    if drive.replaced.is_set() or self.host.closed:
                        return
                    await self._call(drive, name, call)
        finally:
            # Cleared before any await so a speech arriving now starts a new run.
            drive.running = False
            self._changed(drive.group.group_id)

    async def _call(self, drive: _Drive, name: str, call: _Call) -> None:
        """A turn ends when the member speaks or its triggered work settles."""
        agent = await self.host.agents.resolve(name)
        if self.host.is_paused(agent.session_id):
            return
        drive.answering[name, call.message_id] = call
        delivery_id = f"attention:{drive.group.group_id}:{call.message_id}:{name}"
        attention = fact_arguments(GroupAttention(group=drive.group.name, message_id=call.message_id),
                                   delivery_id=delivery_id)
        spoke = drive.waiting[name] = asyncio.Event()
        self._changed(drive.group.group_id)
        try:
            await self.host.request("fact", agent.session_id, attention)
            if await self._behind_running_commands(agent.session_id, delivery_id):
                # The member answers once its running commands finish; nobody waits behind it.
                return
            waits = [asyncio.create_task(self.host.wait_quiescent(agent.session_id)),
                     asyncio.create_task(spoke.wait()), asyncio.create_task(drive.replaced.wait())]
            done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            del drive.waiting[name]
            self._changed(drive.group.group_id)

    async def _behind_running_commands(self, session_id: str, delivery_id: str) -> bool:
        """Whether a delivered call must wait for commands the member is already running.

        Read from the member's Journal, not from whether its Worker is up: a Worker
        started just to accept a group message is not busy.
        """
        events = await SqliteJournal(self.host.store.require(session_id)).snapshot(session_id)
        state = replay(session_id, events).state
        attention = next(event.event_id for event in events
                         if event.delivery is not None and event.delivery.delivery_id == delivery_id)
        handled = {step.step.trigger_event_id for step in state.steps} | {
            event.payload.trigger_event_id for event in events if isinstance(event.payload, DecisionCancelled)}
        # Pending with nothing ready to decide: it is queued behind running commands.
        return state.next_trigger_event_id is None and attention not in handled

    async def send(self, session_id: str, arguments: dict, origin: str, admit):
        """An Agent speech. ``admit`` checks the per-origin budget once the send is known to be new.

        The speech answers a call only when its ``reply_to`` quotes the message the
        sender was called to answer and that call is still open; each call is
        answered once. Which message a speech answers is the model's statement and
        is never inferred from what started its work.
        """
        sender = await self.host.agents.by_session(session_id)
        group = next((group for group in await self.list()
                      if group.name == arguments["group"]), None)
        if group is None:
            return {"ok": False, "code": "GROUP_NOT_FOUND", "error": "没有这个群。"}
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
                return self._delivered(group, identity, group.group_id in self._drives)
            if arguments["reply_to"] is not None and all(
                    message.message_id != arguments["reply_to"] for message in messages):
                return {"ok": False, "code": "INVALID_REPLY", "error": "引用的消息必须是这个群里的消息。"}
            refused = await admit(group.name)
            if refused is not None:
                return refused
            drive = self._drives.get(group.group_id)
            if drive is None:
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
            if answered is None:
                calls = drive.queue(sender.name, mention, _Call(identity, identity, 1))
            else:
                drive.replies[answered.opener] += 1
                calls = drive.queue(sender.name, mention, _Call(identity, answered.opener, answered.round + 1))
            self._schedule(drive)
            return self._delivered(group, identity, calls)

    def _delivered(self, group, identity, calls):
        notice = ("已送达；被叫到的成员会依次回应，不等待回复。" if calls else
                  "已送达；这次群讨论已达轮数上限，不会再叫成员回应；需要某位同事接着处理时可以私信他。")
        return {"ok": True, "code": "DELIVERED", "data": {"message_id": identity, "group": group.name,
                "notice": notice}}
