"""Host delivery and per-connection history over the Bot's existing Journal."""
from __future__ import annotations

import asyncio
from hashlib import sha256

from redpanda.assistant.external_messages import (
    EXTERNAL_MESSAGE, ConnectionStore, ExternalMessage, parse_external,
)
from redpanda.assistant.host.agent_messages import message_identity
from redpanda.assistant.host.agent_roster import AgentDeletedError
from redpanda.runtime import DomainFactCommitted, SqliteJournal


class ExternalMessages:
    def __init__(self, host):
        self.host = host
        self.connections = ConnectionStore(host.home.root / "external_connections.json")
        self._sending = asyncio.Lock()

    async def messages(self, session_id):
        return [(event, parse_external(event.payload))
                for event in await SqliteJournal(self.host.store.require(session_id)).snapshot(session_id)
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == EXTERNAL_MESSAGE]

    async def context(self, connection):
        entry = await self.host.agents.by_session(connection.session_id)
        if entry is None or entry.deleted:
            return {"ok": False, "code": "RECIPIENT_NOT_FOUND", "error": "接入的 Bot 已不存在，请联系用户重新配置。"}
        colleagues = await self.host.agents.list()
        direction = next(item.direction for item in colleagues if item.agent.session_id == connection.session_id)
        return {"ok": True, "name": connection.name, "bot": entry.name, "direction": direction,
                "notice": "你可以向这位 Bot 发送消息并读取双方的交流；发送后回复可能稍后到达。"}

    async def send(self, connection, arguments):
        identity = "external-" + sha256(f"{connection.connection_id}\0{arguments.delivery_id}".encode()).hexdigest()
        message = ExternalMessage(connection_id=connection.connection_id, name=connection.name,
            message_id=identity, reply_to=None, text=arguments.text,
            revision=arguments.revision, reference=arguments.reference)
        async with self._sending:
            existing = next((fact for _, fact in await self.messages(connection.session_id)
                             if fact.message_id == identity), None)
            if existing is not None:
                if existing != message:
                    return {"ok": False, "code": "DELIVERY_CONFLICT",
                            "error": "delivery_id 已用于另一条消息。重试原消息请保持原内容，新消息请使用新的 delivery_id。"}
                return self.delivered(identity)
            context = await self.context(connection)
            if not context["ok"]:
                return context
            try:
                await self.host.request("fact", connection.session_id, message.arguments())
            except AgentDeletedError:
                return {"ok": False, "code": "RECIPIENT_NOT_FOUND", "error": "接入的 Bot 已被删除。"}
        return self.delivered(identity)

    async def read(self, connection, after, limit):
        messages = [(event, fact) for event, fact in await self.messages(connection.session_id)
                    if fact.connection_id == connection.connection_id]
        # Cursor counts only this collaborator's messages; unrelated private activity stays private.
        if after > len(messages):
            return {"ok": False, "code": "INVALID_CURSOR", "error": "游标超出当前消息范围，请使用上次返回的 next_cursor。"}
        page = messages[after:after + limit]
        return {"ok": True, "messages": [dict(fact.model_dump(exclude={"connection_id"}),
                sender={"kind": "external" if fact.incoming else "agent", "name": fact.name},
                occurred_at=event.occurred_at.isoformat()) for event, fact in page],
                "next_cursor": after + len(page), "has_more": after + len(page) < len(messages)}

    async def reply(self, session_id, arguments):
        async with self._sending:
            messages = await self.messages(session_id)
            original = next((fact for _, fact in messages
                             if fact.message_id == arguments["reply_to"] and fact.incoming), None)
            if original is None:
                return {"ok": False, "code": "INVALID_REPLY", "error": "reply_to 必须引用你收到的外部消息。"}
            identity = message_identity(session_id, arguments["command_id"])
            existing = next((fact for _, fact in messages if fact.message_id == identity), None)
            if existing is not None:
                if existing.reply_to != original.message_id or existing.text != arguments["text"]:
                    raise ValueError("persisted external reply conflicts with its Command")
                return self.delivered(identity)
            connection = next((item for item in self.connections.read().connections
                               if item.connection_id == original.connection_id and item.session_id == session_id), None)
            if connection is None:
                return {"ok": False, "code": "CONNECTION_REVOKED", "error": "用户已撤销此协作者的接入，无法回复。"}
            sender = await self.host.agents.by_session(session_id)
            message = ExternalMessage(connection_id=original.connection_id, name=sender.name,
                message_id=identity, reply_to=original.message_id, text=arguments["text"],
                revision=original.revision, reference=original.reference)
            await self.host.request("fact", session_id, message.arguments())
            return self.delivered(identity)

    @staticmethod
    def delivered(identity):
        return {"ok": True, "code": "DELIVERED", "data": {"message_id": identity,
                "notice": "已送达；对方可在下次读取消息时看到。"}}
