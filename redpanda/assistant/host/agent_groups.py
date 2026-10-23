"""Host-owned group records and deterministic turn scheduling for group chats."""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from redpanda.assistant.agent_messages import (
    MAX_GROUP_REPLIES, MAX_GROUP_ROUNDS,
)
from redpanda.assistant.agents import (
    GROUP_CREATED, GROUP_DISSOLVED, GROUP_MESSAGE, GroupAttention, GroupCreated, GroupDissolved, GroupMessage, Speaker,
    fact_arguments, parse_agent_fact,
)
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.agent_roster import AgentDeletedError
from redpanda.runtime import AgentRuntime, DecisionCancelled, DomainFactCommitted, SqliteJournal, replay
from redpanda.runtime.json_values import thaw_value

_DRIVE = "group.drive"


class GroupNameTaken(ValueError):
    pass


class GroupNotFound(LookupError):
    pass


class GroupDissolvedError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class GroupRecord:
    group_id: str
    created: GroupCreated
    dissolved: bool = False

    @property
    def name(self) -> str:
        return self.created.group

    @property
    def members(self) -> list[str]:
        """Names at creation remain the keys of already accepted group turns."""
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
    batch: dict[str, _Call] = field(default_factory=dict)
    active: str | None = None
    outbox: dict[str, GroupMessage] = field(default_factory=dict)
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

    async def _load(self, group: GroupRecord) -> _Drive:
        if group.group_id in self._drives:
            return self._drives[group.group_id]
        drive = _Drive(group)
        events = await self._journal(group.group_id).snapshot(group.group_id)
        saved = [event.payload.data for event in events if isinstance(event.payload, DomainFactCommitted)
                 and event.payload.fact_type == _DRIVE]
        if saved:
            state = thaw_value(saved[-1])
            drive.replies = Counter(state["replies"])
            drive.pending = {name: _Call(**call) for name, call in state["pending"]}
            drive.batch = {name: _Call(**call) for name, call in state["batch"]}
            drive.active = state["active"]
            drive.answering = {(name, call["message_id"]): _Call(**call) for name, call in state["answering"]}
            drive.outbox = {item["message_id"]: GroupMessage.model_validate(item) for item in state["outbox"]}
        self._drives[group.group_id] = drive
        return drive

    async def _save(self, drive: _Drive, *, receipt: tuple[str, bool] | None = None) -> None:
        # One durable group fact accepts both the speech and its scheduling effects.
        # Public/member copies can then be completed using their original delivery id.
        data = dict(replies=dict(drive.replies), pending=[(name, asdict(call)) for name, call in drive.pending.items()],
                    batch=[(name, asdict(call)) for name, call in drive.batch.items()], active=drive.active,
                    answering=[(name, asdict(call)) for (name, _), call in drive.answering.items()],
                    outbox=[message.model_dump(mode="json") for message in drive.outbox.values()],
                    receipt=None if receipt is None else list(receipt))
        await AgentRuntime(self._journal(drive.group.group_id), None, {}).receive_domain_fact(
            drive.group.group_id, _DRIVE, data, source="group", delivery_id=uuid4().hex)

    async def _flush(self, drive: _Drive) -> None:
        for identity, message in list(drive.outbox.items()):
            await self._publish(drive.group, message)
            del drive.outbox[identity]
            await self._save(drive)

    async def prepare_recovery(self) -> None:
        # Publish all accepted source records before any Worker can rebuild origin budgets.
        for group in await self.list():
            drive = await self._load(group)
            for message in drive.outbox.values():
                await AgentRuntime(self._journal(group.group_id), None, {}).receive_domain_fact(
                    group.group_id, **fact_arguments(message, delivery_id=message.message_id))

    async def recover(self) -> None:
        await self.prepare_recovery()
        for group in await self.list(include_dissolved=True):
            async with self._group_locks.setdefault(group.group_id, asyncio.Lock()):
                for name in group.members:
                    agent = await self.host.agents.resolve_recorded(name)
                    if not agent.deleted:
                        fact = GroupDissolved(group=group.name) if group.dissolved else group.created
                        await self._deliver_member(agent.session_id, fact_arguments(
                            fact, delivery_id=f"dissolved:{group.group_id}" if group.dissolved else f"joined:{group.group_id}"))
                if group.dissolved:
                    continue
                drive = self._drives[group.group_id]
                await self._flush(drive)
                self._schedule(drive)

    async def _receipt(self, group_id: str, identity: str) -> bool | None:
        for event in await self._journal(group_id).snapshot(group_id):
            if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == _DRIVE:
                receipt = event.payload.data["receipt"]
                if receipt is not None and receipt[0] == identity:
                    return receipt[1]
        return None

    def _journal(self, group_id):
        return SqliteJournal(self.store.require(group_id))

    async def list(self, *, include_dissolved=False) -> tuple[GroupRecord, ...]:
        groups = []
        for path in self.store.journals():
            journal = SqliteJournal(path)
            group_id = await journal.session_identity()
            events = await journal.snapshot(group_id)
            created, = (parse_agent_fact(event.payload) for event in events
                        if isinstance(event.payload, DomainFactCommitted)
                        and event.payload.fact_type == GROUP_CREATED)
            dissolved = bool([parse_agent_fact(event.payload) for event in events
                              if isinstance(event.payload, DomainFactCommitted)
                              and event.payload.fact_type == GROUP_DISSOLVED])
            if include_dissolved or not dissolved:
                groups.append(GroupRecord(group_id, created, dissolved))
        return tuple(sorted(groups, key=lambda group: group.name))

    async def get(self, group_id: str) -> GroupRecord:
        for group in await self.list(include_dissolved=True):
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
            "dissolved": group.dissolved,
            "members": await self.member_names(group),
            "calling": [(await self.host.agents.resolve_recorded(name)).name for name in drive.waiting]
                       if drive is not None and drive.running and not group.dissolved else [],
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

    async def member_names(self, group: GroupRecord) -> list[str]:
        agents = [await self.host.agents.resolve_recorded(name) for name in group.members]
        return [agent.name for agent in agents if not agent.deleted]

    async def _member_keys(self, group: GroupRecord, names: list[str]) -> list[str]:
        agents = [(name, await self.host.agents.resolve_recorded(name)) for name in group.members]
        return [next(key for key, agent in agents if agent.name == name and not agent.deleted) for name in names]

    async def create(self, *, name: str, members: list[str]) -> GroupRecord:
        created = GroupCreated(group=name, members=members)
        agents = [await self.host.agents.resolve(member) for member in created.members]
        group_id = f"group-{uuid4().hex}"
        async with self._creation_lock:
            if any(group.name == name for group in await self.list(include_dissolved=True)):
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
        recipients = [await self.host.agents.resolve_recorded(name)
                      for name in group.members]
        recipients = [agent for agent in recipients if not agent.deleted and message.sender.name not in agent.names]
        await asyncio.gather(*(self._deliver_member(agent.session_id, arguments) for agent in recipients))
        self._changed(group.group_id)

    async def _deliver_member(self, session_id, arguments):
        try:
            await self.host.request("fact", session_id, arguments)
        except AgentDeletedError:
            # The person may delete a member while this accepted message is being copied.
            return

    async def dissolve(self, group_id: str) -> None:
        async with self._group_locks.setdefault(group_id, asyncio.Lock()):
            group = await self.get(group_id)
            if not group.dissolved:
                drive = await self._load(group)
                await self._flush(drive)
                await AgentRuntime(self._journal(group_id), None, {}).receive_domain_fact(group_id,
                    **fact_arguments(GroupDissolved(group=group.name), delivery_id="dissolved"))
                drive.replaced.set()
            for name in group.members:
                agent = await self.host.agents.resolve_recorded(name)
                if not agent.deleted:
                    await self._deliver_member(agent.session_id, fact_arguments(
                        GroupDissolved(group=group.name), delivery_id=f"dissolved:{group_id}"))
            self._changed(group_id)

    def _changed(self, group_id: str) -> None:
        """Tell observers the record or who is being called changed; never delays scheduling."""
        if self.host.group_changed_sink is not None:
            self.host._track(group_id, asyncio.create_task(self._notify(group_id)))

    async def _notify(self, group_id: str) -> None:
        observed = self.host.group_changed_sink(group_id)
        if isinstance(observed, Awaitable):
            await observed

    async def post(self, group_id: str, text: str, mention: list[str], *, delivery_id: str | None = None) -> GroupMessage:
        """A human message starts a new drive, superseding the previous one."""
        group = await self.get(group_id)
        members = await self.member_names(group)
        if any(name not in members for name in mention):
            raise ValueError("点名的成员不在这个群里")
        message = GroupMessage(message_id=f"group-message-{delivery_id or uuid4().hex}", sender=Speaker(kind="user", name=None),
                               reply_to=None, text=text, group=group.name, mentioned_agents=mention, origin=None)
        async with self._group_locks.setdefault(group_id, asyncio.Lock()):
            if (await self.get(group_id)).dissolved:
                raise GroupDissolvedError(group_id)
            previous = await self._load(group)
            await self._flush(previous)
            existing = next((item for item in await self.messages(group_id) if item.message_id == message.message_id), None)
            if existing is not None:
                if existing != message:
                    raise ValueError("group message delivery identity conflicts with its content")
                self._schedule(previous)
                return existing
            drive = self._replace_drive(group)
            drive.queue(None, await self._member_keys(group, mention), _Call(message.message_id, message.message_id, 1))
            drive.outbox[message.message_id] = message
            await self._save(drive, receipt=(message.message_id, True))
            await self._flush(drive)
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
        if (drive.pending or drive.batch) and not drive.replaced.is_set() and not drive.running:
            drive.running = True
            self.host._track(drive.group.group_id, asyncio.create_task(self._run(drive)))

    async def _run(self, drive: _Drive) -> None:
        try:
            while (drive.pending or drive.batch) and not drive.replaced.is_set():
                async with self._group_locks[drive.group.group_id]:
                    if drive.replaced.is_set() or self.host.closed:
                        return
                    if not drive.batch:
                        drive.batch, drive.pending = drive.pending, {}
                    name, call = next(iter(drive.batch.items()))
                    if drive.active is None:
                        drive.active = name
                        drive.answering[name, call.message_id] = call
                        await self._save(drive)
                await self._call(drive, name, call)
                async with self._group_locks[drive.group.group_id]:
                    if drive.replaced.is_set():
                        return
                    del drive.batch[name]
                    drive.active = None
                    await self._save(drive)
        finally:
            # Cleared before any await so a speech arriving now starts a new run.
            drive.running = False
            self._changed(drive.group.group_id)

    async def _call(self, drive: _Drive, name: str, call: _Call) -> None:
        """A turn ends when the member speaks or its triggered work settles."""
        agent = await self.host.agents.resolve_recorded(name)
        if agent.deleted:
            return
        delivery_id = f"attention:{drive.group.group_id}:{call.message_id}:{name}"
        events = await SqliteJournal(self.host.store.require(agent.session_id)).snapshot(agent.session_id)
        accepted = next((event.event_id for event in events if event.delivery is not None
                         and event.delivery.delivery_id == delivery_id), None)
        if accepted is not None:
            state = replay(agent.session_id, events).state
            handled = {step.step.trigger_event_id for step in state.steps} | {
                event.payload.trigger_event_id for event in events if isinstance(event.payload, DecisionCancelled)}
            if accepted in handled:
                return
        attention = fact_arguments(GroupAttention(group=drive.group.name, message_id=call.message_id),
                                   delivery_id=delivery_id)
        spoke = drive.waiting[name] = asyncio.Event()
        waits = []
        self._changed(drive.group.group_id)
        try:
            try:
                await self.host.request("fact", agent.session_id, attention)
            except AgentDeletedError:
                return
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
            for task in waits:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
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
        members = await self.member_names(group)
        if sender.name not in members:
            return {"ok": False, "code": "NOT_A_MEMBER", "error": "你不在这个群里。"}
        mention = arguments["mention"]
        async with self._group_locks.setdefault(group.group_id, asyncio.Lock()):
            if (await self.get(group.group_id)).dissolved:
                return {"ok": False, "code": "GROUP_NOT_FOUND", "error": "这个群已解散，不能再向它发言。"}
            drive = await self._load(group)
            await self._flush(drive)
            messages = await self.messages(group.group_id)
            identity = arguments["message_id"]
            existing = next((message for message in messages if message.message_id == identity), None)
            if existing is not None:
                if (existing.sender.name not in sender.names or existing.group != group.name
                        or existing.text != arguments["text"] or existing.mentioned_agents != mention
                        or existing.reply_to != arguments["reply_to"]):
                    raise ValueError("persisted group message conflicts with its sending Command")
                await self._publish(group, existing)
                self._schedule(drive)
                return self._delivered(group, identity, await self._receipt(group.group_id, identity))
            if any(name not in members or name == sender.name for name in mention):
                return {"ok": False, "code": "INVALID_MENTION", "error": "只能点名这个群里的其他成员。"}
            if arguments["reply_to"] is not None and all(
                    message.message_id != arguments["reply_to"] for message in messages):
                return {"ok": False, "code": "INVALID_REPLY", "error": "引用的消息必须是这个群里的消息。"}
            refused = await admit(group.name)
            if refused is not None:
                return refused
            speaker, = await self._member_keys(group, [sender.name])
            mentioned = await self._member_keys(group, mention)
            answered = drive.answering.get((speaker, arguments["reply_to"]))
            if answered is not None and drive.replies[answered.opener] >= MAX_GROUP_REPLIES:
                return {"ok": False, "code": "GROUP_LIMIT_REACHED",
                        "error": "这次群讨论的回复已达上限；需要某位同事接着处理时可以私信他。"}
            message = GroupMessage(message_id=identity, sender=Speaker(kind="agent", name=sender.name),
                                   reply_to=arguments["reply_to"], text=arguments["text"], group=group.name,
                                   mentioned_agents=mention, origin=origin)
            if answered is not None:
                del drive.answering[speaker, arguments["reply_to"]]
            # Any speech from a member being called ends the Host's wait for it.
            if answered is None:
                calls = drive.queue(speaker, mentioned, _Call(identity, identity, 1))
            else:
                drive.replies[answered.opener] += 1
                calls = drive.queue(speaker, mentioned, _Call(identity, answered.opener, answered.round + 1))
            drive.outbox[identity] = message
            await self._save(drive, receipt=(identity, calls))
            await self._flush(drive)
            if speaker in drive.waiting:
                drive.waiting[speaker].set()
            self._schedule(drive)
            return self._delivered(group, identity, calls)

    def _delivered(self, group, identity, calls):
        notice = ("已送达。" if calls is None else "已送达；被叫到的成员会依次回应，不等待回复。" if calls else
                  "已送达；这次群讨论已达轮数上限，不会再叫成员回应；需要某位同事接着处理时可以私信他。")
        return {"ok": True, "code": "DELIVERED", "data": {"message_id": identity, "group": group.name,
                "notice": notice}}
