"""Verification delivery and feedback over the existing Bot timelines."""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from uuid import uuid4

from redpanda.assistant.external_messages import (
    EXTERNAL_MESSAGE, ConnectionStore, ExternalMessage, Feedback, external_name, parse_external,
)
from redpanda.assistant.host.agent_messages import message_identity
from redpanda.assistant.host.agent_roster import AgentDeletedError, AgentNotFound
from redpanda.runtime import DomainFactCommitted, SqliteJournal


class ExternalConnectionNotFound(LookupError):
    pass


class ExternalMessages:
    def __init__(self, host):
        self.host = host
        self.connections = ConnectionStore(host.home.root / "external_connections.json")
        self._sending = asyncio.Lock()
        self._changed = asyncio.Condition(self._sending)

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

    def _active(self, connection):
        return any(item.connection_id == connection.connection_id for item in self.connections.read().connections)

    async def messages(self, session_id):
        return [(event, parse_external(event.payload))
                for event in await SqliteJournal(self.host.store.require(session_id)).snapshot(session_id)
                if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == EXTERNAL_MESSAGE]

    async def _request(self, connection, identity, request_id):
        for entry in await self.host.agents.list(include_deleted=True):
            messages = await self.messages(entry.agent.session_id)
            for _, fact in messages:
                if (fact.incoming and fact.message_id == request_id
                        and fact.connection_id == connection.connection_id and fact.identity == identity):
                    return entry.agent, fact, messages
        return None

    async def context(self, connection, identity=None):
        identity = uuid4().hex if identity is None else identity
        return {"ok": True, "identity": identity, "name": external_name(identity),
                "colleagues": [{"name": item.agent.name, "direction": item.direction}
                               for item in await self.host.agents.list()],
                "notice": ("根据同事分工选择验证对象，首次 request_verification 填 recipient。"
                           "同一编码会话保留 identity；提交需求、PR/仓库引用与完整版本后，用 read_feedback 等待报告。"
                           "用户无需替你取名或指定固定交接人。")}

    async def send(self, connection, arguments):
        message_id = "external-" + sha256(json.dumps(
            [connection.connection_id, arguments.identity, arguments.delivery_id]).encode()).hexdigest()
        async with self._sending:
            if not self._active(connection):
                return self.revoked()
            previous = None
            if arguments.previous_request_id is not None:
                previous = await self._request(connection, arguments.identity, arguments.previous_request_id)
                if previous is None:
                    return self.not_found()
            if arguments.recipient is None:
                recipient = previous[0]
            else:
                try:
                    recipient = await self.host.agents.resolve(arguments.recipient)
                except AgentNotFound:
                    return {"ok": False, "code": "RECIPIENT_NOT_FOUND",
                            "error": "没有这位验证同事，请重新调用 get_context 查询。"}
            if recipient.deleted:
                return {"ok": False, "code": "RECIPIENT_NOT_FOUND", "error": "原验证同事已退出，请查询并指定其他同事。"}
            message = ExternalMessage(connection_id=connection.connection_id, name=external_name(arguments.identity),
                identity=arguments.identity, message_id=message_id, reply_to=None, text=arguments.text,
                revision=arguments.revision, reference=arguments.reference,
                previous_request_id=arguments.previous_request_id, feedback=None)
            existing = await self._request(connection, arguments.identity, message_id)
            if existing is not None:
                if existing[0].session_id != recipient.session_id or existing[1] != message:
                    return {"ok": False, "code": "DELIVERY_CONFLICT",
                            "error": "delivery_id 已用于另一项提交；重试保持所有内容不变，新提交使用新标识。"}
                return self.accepted(message, recipient.name)
            try:
                await self.host.request("fact", recipient.session_id, message.arguments())
            except AgentDeletedError:
                return {"ok": False, "code": "RECIPIENT_NOT_FOUND", "error": "这位验证同事已退出。"}
            return self.accepted(message, recipient.name)

    async def _read(self, connection, identity, request_id, after, limit):
        if not self._active(connection):
            return self.revoked()
        found = await self._request(connection, identity, request_id)
        if found is None:
            return self.not_found()
        agent, original, messages = found
        feedback = [(event, fact) for event, fact in messages if fact.reply_to == original.message_id]
        if after > len(feedback):
            return {"ok": False, "code": "INVALID_CURSOR", "error": "请使用这项请求上次返回的 next_cursor。"}
        page = feedback[after:after + limit]
        return {"ok": True, "request": {"request_id": original.message_id, "recipient": agent.name,
                "text": original.text, "revision": original.revision, "reference": original.reference,
                "previous_request_id": original.previous_request_id},
                "feedback": [dict(fact.model_dump(exclude={"connection_id", "identity", "previous_request_id"}),
                    occurred_at=event.occurred_at.isoformat()) for event, fact in page],
                "next_cursor": after + len(page), "has_more": after + len(page) < len(feedback),
                "notice": "进度或问题不代表验证结束。报告是验证 Bot 提交的结论；取得问题后修复，再带新版本与 previous_request_id 请求复验。"}

    async def read(self, connection, identity, request_id, after, limit, wait_seconds):
        deadline = asyncio.get_running_loop().time() + wait_seconds
        async with self._changed:
            while True:
                result = await self._read(connection, identity, request_id, after, limit)
                remaining = deadline - asyncio.get_running_loop().time()
                if not result["ok"] or result["feedback"] or remaining <= 0:
                    return result
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except TimeoutError:
                    return await self._read(connection, identity, request_id, after, limit)

    async def reply(self, session_id, arguments):
        async with self._changed:
            messages = await self.messages(session_id)
            original = next((fact for _, fact in messages
                             if fact.message_id == arguments["reply_to"] and fact.incoming), None)
            if original is None:
                return {"ok": False, "code": "INVALID_REPLY", "error": "reply_to 必须引用你收到的验证请求。"}
            message_id = message_identity(session_id, arguments["command_id"])
            feedback = Feedback(**{key: arguments[key] for key in Feedback.model_fields})
            existing = next((fact for _, fact in messages if fact.message_id == message_id), None)
            if existing is not None:
                if (existing.reply_to != original.message_id or existing.text != arguments["text"]
                        or existing.feedback != feedback):
                    raise ValueError("persisted verification feedback conflicts with its Command")
                return self.delivered(message_id)
            if not any(item.connection_id == original.connection_id for item in self.connections.read().connections):
                return self.revoked()
            sender = await self.host.agents.by_session(session_id)
            message = ExternalMessage(connection_id=original.connection_id, name=sender.name,
                identity=original.identity, message_id=message_id, reply_to=original.message_id, text=arguments["text"],
                revision=original.revision, reference=original.reference,
                previous_request_id=original.previous_request_id, feedback=feedback)
            await self.host.request("fact", session_id, message.arguments())
            self._changed.notify_all()
            return self.delivered(message_id)

    @staticmethod
    def accepted(message, recipient):
        return {"ok": True, "code": "ACCEPTED", "request_id": message.message_id, "recipient": recipient,
                "revision": message.revision,
                "notice": "已交付验证；调用 read_feedback 等待并读取报告。已接纳不表示已验证，请在当前编码会话中接着处理反馈。"}

    @staticmethod
    def delivered(message_id):
        return {"ok": True, "code": "DELIVERED", "data": {"message_id": message_id,
                "notice": "反馈已送达，原编码会话可以读取；不表示对方已处理。"}}

    @staticmethod
    def not_found():
        return {"ok": False, "code": "REQUEST_NOT_FOUND", "error": "没有这项请求，请使用当前会话实际收到的 request_id。"}

    @staticmethod
    def revoked():
        return {"ok": False, "code": "CONNECTION_REVOKED", "error": "用户已撤销验证接入，不能继续收发。"}
