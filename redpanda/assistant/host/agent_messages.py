"""Host-owned addressing, reply validation, origin budgets and durable delivery to colleagues."""
from __future__ import annotations

import asyncio
from hashlib import sha256

from redpanda.assistant.agent_messages import LIMIT_NOTICE, MAX_MESSAGE_HOPS, MAX_MESSAGES_PER_ORIGIN
from redpanda.assistant.agents import (
    PRIVATE_MESSAGE, ColleagueJoined, DirectionDeclared, PrivateMessage, Speaker, fact_arguments, message_origin,
    parse_agent_fact,
)
from redpanda.assistant.conversations import project_agent_roster_entry
from redpanda.assistant.host.agent_roster import AgentNotFound
from redpanda.runtime import DomainFactCommitted, SqliteJournal


# The Web roster tells "waiting for the person" apart from idle; to a colleague both
# mean the same thing: not working now, and a message reaches it.
COLLEAGUE_STATUS = {"working": "working", "authorization": "awaiting_authorization",
                    "waiting": "idle", "idle": "idle"}


def message_identity(session_id, command_id):
    digest = sha256(f"{session_id}\0{command_id}".encode()).hexdigest()
    return f"message-{digest}"


class OriginUsage:
    """Which Agent messages each origin has driven, indexed once and kept current.

    Message ids make recording idempotent, so redelivery during recovery never
    counts twice.
    """

    def __init__(self, host):
        self.host = host
        self._used: dict[str, set[str]] | None = None
        self._loading = asyncio.Lock()

    async def _index(self):
        async with self._loading:
            if self._used is None:
                used: dict[str, set[str]] = {}
                for entry in await self.host.agents.list():
                    events = await SqliteJournal(self.host.store.require(entry.agent.session_id)).snapshot(
                        entry.agent.session_id)
                    for event in events:
                        if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == PRIVATE_MESSAGE:
                            fact = parse_agent_fact(event.payload)
                            used.setdefault(fact.origin, set()).add(fact.message_id)
                for group in await self.host.agent_groups.list():
                    for message in await self.host.agent_groups.messages(group.group_id):
                        if message.origin is not None:
                            used.setdefault(message.origin, set()).add(message.message_id)
                self._used = used
            return self._used

    async def count(self, origin: str) -> int:
        return len((await self._index()).get(origin, ()))

    async def record(self, origin: str, message_id: str) -> None:
        (await self._index()).setdefault(origin, set()).add(message_id)


class AgentMessages:
    def __init__(self, host):
        self.host = host
        self._sending = asyncio.Lock()
        self.origins = OriginUsage(host)

    async def colleagues(self, session_id):
        sender = await self.host.agents.by_session(session_id)
        if sender is None:
            raise ValueError("colleague query requires a Coding Bot identity")
        return {"ok": True, "code": "COLLEAGUES_READ", "data": {"colleagues": [
            await self._colleague(entry) for entry in await self.host.agents.list()
            if entry.agent.session_id != session_id]}}

    async def _colleague(self, entry):
        """The same status the Web roster shows, plus the execution stop; reads this colleague's Journal."""
        agent = entry.agent
        events = await SqliteJournal(self.host.store.require(agent.session_id)).snapshot(agent.session_id)
        if self.host.is_execution_stopped(agent.session_id):
            status = "stopped"
        else:
            status = COLLEAGUE_STATUS[project_agent_roster_entry(
                agent.session_id, events, name=agent.name,
                direction=entry.direction, activity=self.host.activity(agent.session_id),
                last_error=self.host.current_failure(agent.session_id),
                auto_authorize=self.host.auto_authorize()).status]
        return {"name": agent.name, "direction": entry.direction, "directory": str(agent.directory),
                "projects": [item.name for item in agent.projects],
                "status": status, "since": events[-1].occurred_at.astimezone().isoformat(timespec="minutes")}

    async def _broadcast(self, sender, fact, delivery_id):
        """Roster changes reach colleagues as facts that do not request a decision."""
        await asyncio.gather(*(
            self.host.request("fact", entry.agent.session_id, fact_arguments(fact, delivery_id=delivery_id))
            for entry in await self.host.agents.list()
            if entry.agent.session_id != sender.session_id))

    async def joined(self, agent, colleagues):
        for session_id in colleagues:
            await self.host.request("fact", session_id, fact_arguments(
                ColleagueJoined(name=agent.name), delivery_id=f"joined:{agent.session_id}"))

    async def declare(self, session_id, arguments):
        sender = await self.host.agents.by_session(session_id)
        if sender is None:
            raise ValueError("direction declaration requires a Coding Bot identity")
        await self.host.agents.declare(session_id, arguments["direction"])
        await self._broadcast(sender, DirectionDeclared(name=sender.name, direction=arguments["direction"]),
                              f"{session_id}:{arguments['delivery_id']}")
        return None

    async def _messages(self, session_id):
        events = await SqliteJournal(self.host.store.require(session_id)).snapshot(session_id)
        return [(event, parse_agent_fact(event.payload)) for event in events
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == PRIVATE_MESSAGE]

    async def _origin(self, session_id, command_id):
        events = await SqliteJournal(self.host.store.require(session_id)).snapshot(session_id)
        return message_origin(events, command_id)

    def _admission(self, origin):
        """One human message (or other non-Agent fact) drives a bounded number of Agent messages."""
        async def admit(recipient):
            if await self.origins.count(origin) >= MAX_MESSAGES_PER_ORIGIN:
                return {"ok": False, "code": "ORIGIN_LIMIT_REACHED", "data": {"recipient": recipient},
                        "error": LIMIT_NOTICE}
            return None
        return admit

    async def send_group(self, session_id, arguments):
        sender = await self.host.agents.by_session(session_id)
        if sender is None:
            raise ValueError("group speaker requires a Coding Bot identity")
        origin = await self._origin(session_id, arguments["command_id"])
        identity = message_identity(session_id, arguments["command_id"])
        async with self._sending:
            result = await self.host.agent_groups.send(
                session_id, {**arguments, "message_id": identity},
                origin, self._admission(origin))
            if result["code"] == "DELIVERED":
                await self.origins.record(origin, identity)
            return result

    async def send(self, session_id, arguments):
        sender = await self.host.agents.by_session(session_id)
        if sender is None:
            raise ValueError("message sender requires a Coding Bot identity")
        async with self._sending:
            return await self._send(sender, session_id, arguments)

    async def _send(self, sender, session_id, arguments):
        name = arguments["recipient"]
        if name == sender.name:
            return {"ok": False, "code": "SELF_MESSAGE", "error": "不能给自己发私信。"}
        try:
            recipient = await self.host.agents.resolve(name)
        except AgentNotFound:
            return {"ok": False, "code": "RECIPIENT_NOT_FOUND", "error": "没有这位同事。"}
        identity = message_identity(session_id, arguments["command_id"])
        received = await self._messages(recipient.session_id)
        existing = next(((event, fact) for event, fact in received if fact.message_id == identity), None)
        if existing is not None:
            event, fact = existing
            if (fact.sender != Speaker(kind="agent", name=sender.name) or fact.recipient != name
                    or fact.text != arguments["text"] or fact.reply_to != arguments["reply_to"]
                    or event.delivery.source != "coding_bot" or event.delivery.delivery_id != identity):
                raise ValueError("persisted message conflicts with its sending Command")
            await self.origins.record(fact.origin, identity)
            return self._delivered(recipient, identity)
        reply_to = arguments["reply_to"]
        previous = None
        if reply_to is not None:
            messages = [*received, *await self._messages(session_id)]
            previous = next((fact for _, fact in messages if fact.message_id == reply_to
                and fact.sender.kind == "agent"
                and (fact.sender.name, fact.recipient) in ((sender.name, name), (name, sender.name))), None)
            if previous is None:
                return {"ok": False, "code": "INVALID_REPLY", "error": "引用的消息必须是你与这位同事之间的私信。"}
        hop = 0 if previous is None else previous.hop + 1
        if hop > MAX_MESSAGE_HOPS:
            return {"ok": False, "code": "HOP_LIMIT_REACHED", "data": {"recipient": name}, "error": LIMIT_NOTICE}
        origin = await self._origin(session_id, arguments["command_id"])
        refused = await self._admission(origin)(name)
        if refused is not None:
            return refused
        message = PrivateMessage(message_id=identity, sender=Speaker(kind="agent", name=sender.name),
            recipient=name, text=arguments["text"], reply_to=reply_to,
            thread_id=identity if previous is None else previous.thread_id, hop=hop, origin=origin)
        await self.host.request("fact", recipient.session_id, fact_arguments(message, delivery_id=identity))
        await self.origins.record(origin, identity)
        return self._delivered(recipient, identity)

    def _delivered(self, recipient, identity):
        execution_stopped = self.host.is_execution_stopped(recipient.session_id)
        return {"ok": True, "code": "DELIVERED", "data": {"message_id": identity,
                "recipient": recipient.name, "execution_stopped": execution_stopped,
                "notice": "已送达；对方当前已停止执行，会在人让它继续后处理。" if execution_stopped else "已送达；不等待对方回复。"}}
