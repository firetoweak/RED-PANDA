"""Durable external identities and bidirectional messages over Bot timelines."""
from __future__ import annotations

import asyncio
from bisect import bisect_right, insort_right
from datetime import datetime, timezone
from hashlib import sha256
import json
from itertools import islice

from konden.assistant.external_messages import (
    EXTERNAL_MESSAGE, USER_SOURCE, ConnectionStore, ExternalMessage, ExternalSession,
    ExternalSessionStore, external_direction, parse_external,
)
from konden.assistant.host.agent_messages import message_identity
from konden.assistant.host.agent_roster import AgentDeletedError, AgentNotFound
from konden.assistant.host.codex_continuation import CodexContinuationInvalid, CodexContinuations, continuation_status
from konden.assistant.host.codex_titles import read_codex_titles
from konden.runtime import AgentRuntime, DomainFactCommitted, Event, UserMessageReceived


class ExternalConnectionNotFound(LookupError):
    pass


class ExternalSessionNotFound(LookupError):
    pass


class ExternalMessageInvalid(ValueError):
    pass


class ExternalMessages:
    def __init__(self, host):
        self.host = host
        self.connections = ConnectionStore(host.home.root / "external_connections.json")
        self.sessions = ExternalSessionStore(host.home.root / "external_sessions.json")
        self.codex = CodexContinuations(host.home.root / "codex_continuations.json")
        self.changed_sink = None
        self._sending = asyncio.Lock()
        self._changed = asyncio.Condition(self._sending)
        self._positions: dict[str, int] = {}
        self._conversations: dict[str, list[tuple[str, Event, ExternalMessage]]] = {}

    async def _notify(self):
        if self.changed_sink is not None:
            await self.changed_sink()

    def list_connections(self):
        return [item.view() for item in self.connections.read().connections]

    def create_connection(self, name):
        connection, token = self.connections.create(name)
        return {"connection": connection.view(), "token": token}

    def connection_configuration(self, connection_id):
        connection = next((item for item in self.connections.read().connections
                           if item.connection_id == connection_id), None)
        if connection is None:
            raise ExternalConnectionNotFound(connection_id)
        return {"connection": connection.view(), "token": connection.token}

    def rename_connection(self, connection_id, name):
        connection = self.connections.rename(connection_id, name)
        if connection is None:
            raise ExternalConnectionNotFound(connection_id)
        return connection.view()

    async def revoke_connection(self, connection_id):
        async with self._changed:
            if not any(item["connection_id"] == connection_id for item in self.list_connections()):
                raise ExternalConnectionNotFound(connection_id)
            self.connections.revoke(connection_id)
            for session in self.sessions.read().sessions:
                if session.connection_id == connection_id:
                    self.codex.configure(session.identity, None)
            self._changed.notify_all()
            await self._notify()

    async def delete_session(self, identity):
        async with self._changed:
            if not any(session.identity == identity for session in self.sessions.read().sessions):
                raise ExternalSessionNotFound(identity)
            self.codex.configure(identity, None)
            self.sessions.remove(identity)
            self._changed.notify_all()
            await self._notify()

    def _active(self, connection_id):
        return any(item.connection_id == connection_id for item in self.connections.read().connections)

    def _session(self, connection_id, identity):
        return next((item for item in self.sessions.read().sessions
                     if item.connection_id == connection_id and item.identity == identity), None)

    async def messages(self, session_id):
        return [(event, parse_external(event.payload))
                for event in await self.host.store.journal(session_id).snapshot(session_id)
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == EXTERNAL_MESSAGE]

    async def recover_user_inputs(self):
        """Complete accepted human inputs before any Bot Worker is resumed."""
        restored = set()
        for agent in await self.host.agents.list(include_deleted=True):
            if agent.deleted:
                continue
            journal = self.host.store.journal(agent.session_id)
            events = await journal.snapshot(agent.session_id)
            received = {event.delivery.delivery_id for event in events
                        if isinstance(event.payload, UserMessageReceived) and event.delivery is not None
                        and event.delivery.source == USER_SOURCE}
            for event in events:
                if not isinstance(event.payload, DomainFactCommitted) or event.payload.fact_type != EXTERNAL_MESSAGE:
                    continue
                message = parse_external(event.payload)
                if message.user_input and message.message_id not in received:
                    await AgentRuntime(journal, None, {}).receive_user_message(agent.session_id,
                        message.user_content(), delivery_id=message.message_id, source=USER_SOURCE)
                    restored.add(agent.session_id)
        return restored

    async def _refresh(self):
        agents = {agent.session_id: agent for agent in await self.host.agents.list(include_deleted=True)}
        for session_id in agents:
            events = await self.host.store.journal(session_id).snapshot(session_id)
            position = self._positions.get(session_id, 0)
            additions = [(event, parse_external(event.payload)) for event in events[position:]
                         if isinstance(event.payload, DomainFactCommitted)
                         and event.payload.fact_type == EXTERNAL_MESSAGE]
            for event, message in additions:
                messages = self._conversations.setdefault(message.identity, [])
                insort_right(messages, (session_id, event, message), key=lambda item: item[2].sequence)
            self._positions[session_id] = max(position, len(events))
        return agents

    def _messages_for(self, session, agents):
        return [(agents[session_id], event, message) for session_id, event, message
                in self._conversations.get(session.identity, ())]

    async def _conversation(self, session):
        return self._messages_for(session, await self._refresh())

    async def _names(self, sessions):
        titles = await read_codex_titles([session.identity for session in sessions if session.label is None])
        return {session.identity: titles.get(session.identity, session.name) for session in sessions}

    def _session_view(self, session, messages, active_connections, bindings, names):
        peers = {agent.session_id: {"name": agent.name, "session_id": agent.session_id, "deleted": agent.deleted}
                 for agent, _, _ in messages}
        latest = next((message.message_id for _, _, message in reversed(messages) if message.calls_codex), None)
        return {"identity": session.identity, "name": names[session.identity],
                "created_at": session.created_at.isoformat(),
                "last_activity_at": max([session.created_at, *(event.occurred_at for _, event, _ in messages)]).isoformat(),
                "active": session.connection_id in active_connections, "peers": list(peers.values()),
                "codex_status": continuation_status(bindings.get(session.identity), latest)}

    async def list_sessions(self):
        sessions = self.sessions.read().sessions
        if not sessions:
            return []
        agents = await self._refresh()
        active = {item.connection_id for item in self.connections.read().connections}
        bindings = self.codex.read().bindings
        names = await self._names(sessions)
        return sorted((self._session_view(session, self._messages_for(session, agents), active, bindings, names)
                       for session in sessions),
                      key=lambda item: item["last_activity_at"], reverse=True)

    async def view(self, identity):
        session = next((item for item in self.sessions.read().sessions if item.identity == identity), None)
        if session is None:
            raise ExternalSessionNotFound(identity)
        messages = await self._conversation(session)
        active = {item.connection_id for item in self.connections.read().connections}
        latest = next((message.message_id for _, _, message in reversed(messages) if message.calls_codex), None)
        names = await self._names([session])
        return {"session": self._session_view(session, messages, active, self.codex.read().bindings, names),
                "codex": self.codex.view(identity, latest), "messages": [
            {"message_id": message.message_id, "sequence": message.sequence,
             "direction": message.direction, "sender": message.sender, "recipients": list(message.recipients),
             "text": message.text, "reply_to": message.reply_to,
             "bot_name": agent.name, "bot_session_id": agent.session_id,
             "occurred_at": event.occurred_at.isoformat()} for agent, event, message in messages]}

    async def configure_codex(self, identity, target):
        session = next((item for item in self.sessions.read().sessions if item.identity == identity), None)
        if session is None:
            raise ExternalSessionNotFound(identity)
        if target is not None and not self._active(session.connection_id):
            raise CodexContinuationInvalid("接入已撤销，不能启用自动接续。")
        self.codex.configure(identity, target)
        await self._notify()
        return await self.view(identity)

    async def retry_codex(self, identity):
        session = next((item for item in self.sessions.read().sessions if item.identity == identity), None)
        if session is None:
            raise ExternalSessionNotFound(identity)
        if not self._active(session.connection_id):
            raise CodexContinuationInvalid("接入已撤销，不能提醒 Codex。")
        if identity not in self.codex.read().bindings:
            raise CodexContinuationInvalid("请先启用 Codex 自动接续。")
        messages = await self._conversation(session)
        latest = next((message.message_id for _, _, message in reversed(messages) if message.calls_codex), None)
        if latest is None:
            raise CodexContinuationInvalid("还没有需要 Codex 处理的消息。")
        await self.codex.notify(identity, latest, retry=True)
        await self._notify()
        return await self.view(identity)

    async def colleagues(self, session_id):
        if await self.host.agents.by_session(session_id) is None:
            raise ValueError("external conversation query requires a Bot identity")
        sessions = await self.list_sessions()
        return {"ok": True, "code": "EXTERNAL_SESSIONS_READ", "data": {"sessions": [
            {key: item[key] for key in ("identity", "name", "active", "last_activity_at", "codex_status")}
            for item in sessions]}}

    async def context(self, connection, identity, name=None):
        async with self._changed:
            if not self._active(connection.connection_id):
                return self.revoked()
            session = next((item for item in self.sessions.read().sessions if item.identity == identity), None)
            if session is None:
                session = ExternalSession(connection_id=connection.connection_id, identity=identity,
                                          created_at=datetime.now(timezone.utc), label=name)
                self.sessions.add(session)
                await self._notify()
            elif session.connection_id != connection.connection_id or (name is not None and name != session.name):
                if session.connection_id != connection.connection_id and self._active(session.connection_id):
                    return {"ok": False, "code": "CONNECTION_CONFLICT",
                            "error": "当前聊天已使用另一份接入配置。请先在 Web 撤销原配置，再通过新配置连接。"}
                session = session.model_copy(update={"connection_id": connection.connection_id,
                    "label": session.label if name is None else name})
                self.sessions.update(session)
                await self._notify()
            names = await self._names([session])
            return {"ok": True, "identity": session.identity, "name": names[session.identity],
                    "colleagues": [{"name": item.name} for item in await self.host.agents.list()],
                    "notice": ("已自动识别当前 Codex 聊天，继续或重连无需保存和传回身份。可用 get_context 的 name 设置便于辨认的名称。"
                               "用 send_message 与同事交谈，用 read_messages 持续收取这个会话的消息。"
                               "同事可以继续发送后续消息；用户可在 Web 为当前会话启用支持的自动接续方式，未启用时需主动读取。")}

    @staticmethod
    def _valid_reply(messages, session_id, reply_to):
        return reply_to is None or any(agent.session_id == session_id and message.message_id == reply_to
                                      for agent, _, message in messages)

    @staticmethod
    def _target(messages, reply_to, explicit, counterpart):
        if explicit is not None:
            return explicit
        original = next((message for _, _, message in messages if message.message_id == reply_to), None)
        return "user" if original is not None and original.sender == "user" else counterpart

    async def post_user(self, identity, arguments):
        message_id = "external-user-" + sha256(json.dumps(
            [identity, arguments.bot_session_id, arguments.delivery_id]).encode()).hexdigest()
        async with self._changed:
            session = next((item for item in self.sessions.read().sessions if item.identity == identity), None)
            if session is None:
                raise ExternalSessionNotFound(identity)
            if not self._active(session.connection_id):
                raise ExternalMessageInvalid("接入已撤销，不能继续发送。")
            bot = await self.host.agents.by_session(arguments.bot_session_id)
            if bot is None or bot.deleted:
                raise ExternalMessageInvalid("这位 Bot 已不可用，请重新选择。")
            messages = await self._conversation(session)
            if not self._valid_reply(messages, bot.session_id, arguments.reply_to):
                raise ExternalMessageInvalid("只能引用这段三方对话中的消息。")
            direction = external_direction("user", arguments.target)
            existing = next((message for _, _, message in messages if message.message_id == message_id), None)
            if existing is not None:
                if (existing.text != arguments.text or existing.direction != direction
                        or existing.reply_to != arguments.reply_to):
                    raise ExternalMessageInvalid("该消息编号已用于不同内容，请作为新消息发送。")
                message = existing
            else:
                message = ExternalMessage(connection_id=session.connection_id, identity=identity, name="用户",
                    message_id=message_id, sequence=len(messages) + 1, direction=direction,
                    reply_to=arguments.reply_to, text=arguments.text)
                if message.user_input and self.host.models is not None:
                    self.host.models.require_ready(bot.session_id)
                await self.host.request("fact", bot.session_id, message.arguments())
            self._changed.notify_all()
            await self._notify()
        if message.user_input:
            await self.host.receive_user_message(bot.session_id, message.user_content(),
                delivery_id=message_id, source=USER_SOURCE)
        if message.calls_codex and await self.codex.notify(identity, message_id):
            await self._notify()
        return await self.view(identity)

    async def send(self, connection, identity, arguments):
        message_id = "external-" + sha256(json.dumps(
            [connection.connection_id, identity, arguments.delivery_id]).encode()).hexdigest()
        async with self._changed:
            if not self._active(connection.connection_id):
                return self.revoked()
            session = self._session(connection.connection_id, identity)
            if session is None:
                return self.not_found()
            messages = await self._conversation(session)
            direction = external_direction("codex", self._target(messages, arguments.reply_to, arguments.target, "bot"))
            existing = next((item for item in messages if item[2].message_id == message_id), None)
            if existing is not None:
                agent, _, message = existing
                if (arguments.recipient not in agent.names or message.text != arguments.text
                        or message.reply_to != arguments.reply_to or message.direction != direction):
                    return {"ok": False, "code": "DELIVERY_CONFLICT",
                            "error": "delivery_id 已用于另一条消息；重试保持内容不变，新消息使用新标识。"}
                return self.delivered(message_id, agent.name)
            try:
                recipient = await self.host.agents.resolve(arguments.recipient)
            except AgentNotFound:
                return {"ok": False, "code": "RECIPIENT_NOT_FOUND", "error": "没有这位同事，请用 get_context 查询当前名字。"}
            if not self._valid_reply(messages, recipient.session_id, arguments.reply_to):
                return self.invalid_reply()
            names = await self._names([session])
            message = ExternalMessage(connection_id=connection.connection_id, identity=session.identity,
                name=names[session.identity], message_id=message_id, sequence=len(messages) + 1, direction=direction,
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
        agents = await self._refresh()
        messages = self._conversations.get(session.identity, ())
        if after > len(messages):
            return {"ok": False, "code": "INVALID_CURSOR", "error": "请使用这个会话上次返回的 next_cursor。"}
        position = bisect_right(messages, after, key=lambda item: item[2].sequence)
        received = (item for item in islice(messages, position, None) if item[2].sender != "codex")
        page = list(islice(received, limit + 1))
        has_more = len(page) > limit
        page = page[:limit]
        return {"ok": True, "messages": [
            {"message_id": message.message_id, "sender": {"kind": message.sender, "name": message.name},
             "bot_name": agents[session_id].name, "recipients": list(message.recipients),
             "response_requested": message.calls_codex,
             "text": message.text, "reply_to": message.reply_to, "occurred_at": event.occurred_at.isoformat()}
            for session_id, event, message in page],
            "next_cursor": page[-1][2].sequence if page else after, "has_more": has_more,
            "notice": "空结果只表示目前没有新消息。保留 next_cursor，继续读取当前聊天的后续消息。"}

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
            direction = external_direction("bot", self._target(messages, arguments["reply_to"], arguments["target"], "codex"))
            message_id = message_identity(session_id, arguments["command_id"])
            existing = next((message for _, _, message in messages if message.message_id == message_id), None)
            if existing is not None:
                if (existing.text != arguments["text"] or existing.reply_to != arguments["reply_to"]
                        or existing.direction != direction):
                    raise ValueError("persisted external message conflicts with its Command")
                message = existing
            else:
                if not self._active(session.connection_id):
                    return self.revoked()
                if not self._valid_reply(messages, session_id, arguments["reply_to"]):
                    return self.invalid_reply()
                sender = await self.host.agents.by_session(session_id)
                if sender is None:
                    raise ValueError("external message sender requires a Bot identity")
                message = ExternalMessage(connection_id=session.connection_id, identity=session.identity,
                    name=sender.name, message_id=message_id, sequence=len(messages) + 1, direction=direction,
                    reply_to=arguments["reply_to"], text=arguments["text"])
                await self.host.request("fact", session_id, message.arguments())
            self._changed.notify_all()
            await self._notify()
        if message.calls_codex and await self.codex.notify(session.identity, message_id):
            await self._notify()
        return self.delivered(message_id, session.name)

    @staticmethod
    def delivered(message_id, recipient):
        return {"ok": True, "code": "DELIVERED", "message_id": message_id, "recipient": recipient,
                "notice": "消息已送达，对方可以读取；不表示对方已经处理。"}

    @staticmethod
    def not_found():
        return {"ok": False, "code": "SESSION_NOT_FOUND",
                "error": ("当前外部会话尚未接入或已被用户删除，不能收发。请告知用户；"
                          "用户要求重新接入时，原生聊天可调用 get_context。Bot 可用 list_external_sessions 查询其他已接入会话。")}

    @staticmethod
    def invalid_reply():
        return {"ok": False, "code": "INVALID_REPLY", "error": "reply_to 必须引用你与这位对话对象之间的消息。"}

    @staticmethod
    def revoked():
        return {"ok": False, "code": "CONNECTION_REVOKED", "error": "用户已撤销接入，不能继续收发。"}
