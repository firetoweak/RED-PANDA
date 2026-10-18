"""Host-owned addressing, reply validation and durable delivery to colleagues."""
from __future__ import annotations

import asyncio
from hashlib import sha256

from redpanda.assistant.agent_messages import MAX_MESSAGE_HOPS, LIMIT_NOTICE
from redpanda.assistant.agents import (
    PRIVATE_MESSAGE, ColleagueJoined, DirectionDeclared, PrivateMessage, Speaker, fact_arguments, parse_agent_fact,
)
from redpanda.assistant.host.agent_roster import AgentNotFound
from redpanda.runtime import DomainFactCommitted, SqliteJournal


def message_identity(session_id, command_id):
    digest = sha256(f"{session_id}\0{command_id}".encode()).hexdigest()
    return f"message-{digest}"


class AgentMessages:
    def __init__(self, host):
        self.host = host

    async def colleagues(self, session_id):
        sender = await self.host.agents.by_session(session_id)
        if sender is None:
            raise ValueError("colleague query requires a Coding Bot identity")
        return {"ok": True, "code": "COLLEAGUES_READ", "data": {"colleagues": [
            {"name": entry.agent.name, "direction": entry.direction}
            for entry in await self.host.agents.list(sender.workspace_id)
            if entry.agent.session_id != session_id]}}

    async def _broadcast(self, sender, fact, delivery_id):
        """Roster changes reach colleagues as facts that do not request a decision."""
        await asyncio.gather(*(
            self.host.request("fact", entry.agent.session_id, fact_arguments(fact, delivery_id=delivery_id))
            for entry in await self.host.agents.list(sender.workspace_id)
            if entry.agent.session_id != sender.session_id))

    async def joined(self, agent):
        await self._broadcast(agent, ColleagueJoined(name=agent.name), f"joined:{agent.session_id}")

    async def declare(self, session_id, arguments):
        sender = await self.host.agents.by_session(session_id)
        if sender is None:
            raise ValueError("direction declaration requires a Coding Bot identity")
        await self._broadcast(sender, DirectionDeclared(name=sender.name, direction=arguments["direction"]),
                              f"{session_id}:{arguments['delivery_id']}")
        return None

    async def _messages(self, session_id):
        events = await SqliteJournal(self.host.store.require(session_id)).snapshot(session_id)
        return [(event, parse_agent_fact(event.payload)) for event in events
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == PRIVATE_MESSAGE]

    async def send(self, session_id, arguments):
        sender = await self.host.agents.by_session(session_id)
        if sender is None:
            raise ValueError("message sender requires a Coding Bot identity")
        name = arguments["recipient"]
        if name == sender.name:
            return {"ok": False, "code": "SELF_MESSAGE", "error": "不能给自己发私信。"}
        try:
            recipient = await self.host.agents.resolve(sender.workspace_id, name)
        except AgentNotFound:
            other_project = any(entry.agent.name == name for entry in await self.host.agents.list())
            return {"ok": False, "code": "DIFFERENT_PROJECT" if other_project else "RECIPIENT_NOT_FOUND",
                    "error": "收件人不在同一个项目。" if other_project else "同项目中没有这位同事。"}
        identity = message_identity(session_id, arguments["command_id"])
        received = await self._messages(recipient.session_id)
        existing = next(((event, fact) for event, fact in received if fact.message_id == identity), None)
        if existing is not None:
            event, fact = existing
            if (fact.sender != Speaker(kind="agent", name=sender.name) or fact.recipient != name
                    or fact.text != arguments["text"] or fact.reply_to != arguments["reply_to"]
                    or event.delivery.source != "coding_bot" or event.delivery.delivery_id != identity):
                raise ValueError("persisted message conflicts with its sending Command")
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
        message = PrivateMessage(message_id=identity, sender=Speaker(kind="agent", name=sender.name),
            recipient=name, text=arguments["text"], reply_to=reply_to,
            thread_id=identity if previous is None else previous.thread_id, hop=hop)
        await self.host.request("fact", recipient.session_id, fact_arguments(message, delivery_id=identity))
        return self._delivered(recipient, identity)

    def _delivered(self, recipient, identity):
        paused = self.host.is_paused(recipient.session_id)
        return {"ok": True, "code": "DELIVERED", "data": {"message_id": identity,
                "recipient": recipient.name, "paused": paused,
                "notice": "已送达；对方当前暂停，会在人让它继续后处理。" if paused else "已送达；不等待对方回复。"}}
