"""Durable external identities and bidirectional messages over Bot timelines."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4

from konden.assistant.external_messages import (
    EXTERNAL_MESSAGE, ConnectionStore, ExternalMessage, ExternalSession,
    ExternalSessionStore, parse_external,
)
from konden.assistant.host.agent_messages import message_identity
from konden.assistant.host.agent_roster import AgentDeletedError, AgentNotFound
from konden.runtime import DomainFactCommitted, SqliteJournal


class ExternalConnectionNotFound(LookupError):
    pass


class ExternalSessionNotFound(LookupError):
    pass


class ExternalMessages:
    def __init__(self, host):
        self.host = host
        self.connections = ConnectionStore(host.home.root / "external_connections.json")
        self.sessions = ExternalSessionStore(host.home.root / "external_sessions.json")
        self.changed_sink = None
        self._sending = asyncio.Lock()
        self._changed = asyncio.Condition(self._sending)

    async def _notify(self):
        if self.changed_sink is not None:
            await self.changed_sink()

    def list_connections(self):
        return [{"connection_id": item.connection_id} for item in self.connections.read().connections]

    def create_connection(self):
        connection, token = self.connections.create()
        return {"connection": {"connection_id": connection.connection_id}, "token": token}

    async def revoke_connection(self, connection_id):
        async with self._changed:
            if not any(item["connection_id"] == connection_id for item in self.list_connections()):
                raise ExternalConnectionNotFound(connection_id)
            self.connections.revoke(connection_id)
            self._changed.notify_all()
            await self._notify()

    def _active(self, connection_id):
        return any(item.connection_id == connection_id for item in self.connections.read().connections)

    def _session(self, connection_id, identity):
        return next((item for item in self.sessions.read().sessions
                     if item.connection_id == connection_id and item.identity == identity), None)

    async def messages(self, session_id):
        return [(event, parse_external(event.payload))
                for event in await SqliteJournal(self.host.store.require(session_id)).snapshot(session_id)
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == EXTERNAL_MESSAGE]

    async def _all_messages(self):
        return [(agent, event, message)
                for agent in await self.host.agents.list(include_deleted=True)
                for event, message in await self.messages(agent.session_id)]

    async def _conversation(self, session):
        return sorted((item for item in await self._all_messages()
                       if item[2].identity == session.identity and item[2].connection_id == session.connection_id),
                      key=lambda item: item[2].sequence)

    def _session_view(self, session, messages):
        peers = {agent.session_id: {"name": agent.name, "session_id": agent.session_id, "deleted": agent.deleted}
                 for agent, _, _ in messages}
        return {"identity": session.identity, "name": session.name,
                "created_at": session.created_at.isoformat(),
                "last_activity_at": max([session.created_at, *(event.occurred_at for _, event, _ in messages)]).isoformat(),
                "active": self._active(session.connection_id), "peers": list(peers.values())}

    async def list_sessions(self):
        messages = await self._all_messages()
        return sorted((self._session_view(session, [item for item in messages
                       if item[2].identity == session.identity and item[2].connection_id == session.connection_id])
                       for session in self.sessions.read().sessions),
                      key=lambda item: item["last_activity_at"], reverse=True)

    async def view(self, identity):
        session = next((item for item in self.sessions.read().sessions if item.identity == identity), None)
        if session is None:
            raise ExternalSessionNotFound(identity)
        messages = await self._conversation(session)
        return {"session": self._session_view(session, messages), "messages": [
            {"message_id": message.message_id, "sequence": message.sequence,
             "direction": message.direction, "text": message.text, "reply_to": message.reply_to,
             "bot_name": agent.name, "bot_session_id": agent.session_id,
             "occurred_at": event.occurred_at.isoformat()} for agent, event, message in messages]}

    async def colleagues(self, session_id):
        if await self.host.agents.by_session(session_id) is None:
            raise ValueError("external conversation query requires a Bot identity")
        sessions = await self.list_sessions()
        return {"ok": True, "code": "EXTERNAL_SESSIONS_READ", "data": {"sessions": [
            {key: item[key] for key in ("identity", "name", "active", "last_activity_at")}
            for item in sessions]}}

    async def context(self, connection, identity=None):
        async with self._changed:
            if not self._active(connection.connection_id):
                return self.revoked()
            if identity is None:
                session = ExternalSession(connection_id=connection.connection_id, identity=uuid4().hex,
                                          created_at=datetime.now(timezone.utc))
                self.sessions.add(session)
                await self._notify()
            else:
                session = self._session(connection.connection_id, identity)
                if session is None:
                    return self.not_found()
            return {"ok": True, "identity": session.identity, "name": session.name,
                    "colleagues": [{"name": item.name} for item in await self.host.agents.list()],
                    "notice": ("这个身份属于当前原生聊天。新聊天省略 identity 领取新身份；同一聊天继续或重连时沿用。"
                               "用 send_message 与同事交谈，用 read_messages 持续收取这个会话的消息。"
                               "同事可以继续发送后续消息；接入不会自动唤醒空闲的原生客户端。")}

    @staticmethod
    def _valid_reply(messages, session_id, reply_to):
        return reply_to is None or any(agent.session_id == session_id and message.message_id == reply_to
                                      for agent, _, message in messages)

    async def send(self, connection, arguments):
        message_id = "external-" + sha256(json.dumps(
            [connection.connection_id, arguments.identity, arguments.delivery_id]).encode()).hexdigest()
        async with self._changed:
            if not self._active(connection.connection_id):
                return self.revoked()
            session = self._session(connection.connection_id, arguments.identity)
            if session is None:
                return self.not_found()
            messages = await self._conversation(session)
            existing = next((item for item in messages if item[2].message_id == message_id), None)
            if existing is not None:
                agent, _, message = existing
                if (arguments.recipient not in agent.names or message.text != arguments.text
                        or message.reply_to != arguments.reply_to):
                    return {"ok": False, "code": "DELIVERY_CONFLICT",
                            "error": "delivery_id 已用于另一条消息；重试保持内容不变，新消息使用新标识。"}
                return self.delivered(message_id, agent.name)
            try:
                recipient = await self.host.agents.resolve(arguments.recipient)
            except AgentNotFound:
                return {"ok": False, "code": "RECIPIENT_NOT_FOUND", "error": "没有这位同事，请用 get_context 查询当前名字。"}
            if not self._valid_reply(messages, recipient.session_id, arguments.reply_to):
                return self.invalid_reply()
            message = ExternalMessage(connection_id=connection.connection_id, identity=session.identity,
                name=session.name, message_id=message_id, sequence=len(messages) + 1, direction="received",
                reply_to=arguments.reply_to, text=arguments.text)
            try:
                await self.host.request("fact", recipient.session_id, message.arguments())
            except AgentDeletedError:
                return {"ok": False, "code": "RECIPIENT_NOT_FOUND", "error": "这位同事已退出。"}
            await self._notify()
            return self.delivered(message_id, recipient.name)

    async def _read(self, connection, identity, after, limit):
        if not self._active(connection.connection_id):
            return self.revoked()
        session = self._session(connection.connection_id, identity)
        if session is None:
            return self.not_found()
        messages = await self._conversation(session)
        if after > len(messages):
            return {"ok": False, "code": "INVALID_CURSOR", "error": "请使用这个会话上次返回的 next_cursor。"}
        received = [item for item in messages if not item[2].incoming and item[2].sequence > after]
        page = received[:limit]
        return {"ok": True, "messages": [
            {"message_id": message.message_id, "sender": {"kind": "bot", "name": message.name},
             "text": message.text, "reply_to": message.reply_to, "occurred_at": event.occurred_at.isoformat()}
            for _, event, message in page],
            "next_cursor": page[-1][2].sequence if page else after, "has_more": len(received) > len(page),
            "notice": "空结果只表示目前没有新消息。保留 identity 和 next_cursor，继续读取后续消息。"}

    async def read(self, connection, identity, after, limit, wait_seconds):
        deadline = asyncio.get_running_loop().time() + wait_seconds
        async with self._changed:
            while True:
                result = await self._read(connection, identity, after, limit)
                remaining = deadline - asyncio.get_running_loop().time()
                if not result["ok"] or result["messages"] or remaining <= 0:
                    return result
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except TimeoutError:
                    return await self._read(connection, identity, after, limit)

    async def send_from_bot(self, session_id, arguments):
        async with self._changed:
            session = next((item for item in self.sessions.read().sessions
                            if item.identity == arguments["identity"]), None)
            if session is None:
                return self.not_found()
            messages = await self._conversation(session)
            message_id = message_identity(session_id, arguments["command_id"])
            existing = next((message for _, _, message in messages if message.message_id == message_id), None)
            if existing is not None:
                if existing.text != arguments["text"] or existing.reply_to != arguments["reply_to"]:
                    raise ValueError("persisted external message conflicts with its Command")
                return self.delivered(message_id, session.name)
            if not self._active(session.connection_id):
                return self.revoked()
            if not self._valid_reply(messages, session_id, arguments["reply_to"]):
                return self.invalid_reply()
            sender = await self.host.agents.by_session(session_id)
            if sender is None:
                raise ValueError("external message sender requires a Bot identity")
            message = ExternalMessage(connection_id=session.connection_id, identity=session.identity,
                name=sender.name, message_id=message_id, sequence=len(messages) + 1, direction="sent",
                reply_to=arguments["reply_to"], text=arguments["text"])
            await self.host.request("fact", session_id, message.arguments())
            self._changed.notify_all()
            await self._notify()
            return self.delivered(message_id, session.name)

    @staticmethod
    def delivered(message_id, recipient):
        return {"ok": True, "code": "DELIVERED", "message_id": message_id, "recipient": recipient,
                "notice": "消息已送达，对方可以读取；不表示对方已经处理。"}

    @staticmethod
    def not_found():
        return {"ok": False, "code": "SESSION_NOT_FOUND",
                "error": "没有这个外部会话。新聊天用 get_context 省略 identity 领取身份；重连使用原身份与连接配置。"}

    @staticmethod
    def invalid_reply():
        return {"ok": False, "code": "INVALID_REPLY", "error": "reply_to 必须引用你与这位对话对象之间的消息。"}

    @staticmethod
    def revoked():
        return {"ok": False, "code": "CONNECTION_REVOKED", "error": "用户已撤销接入，不能继续收发。"}