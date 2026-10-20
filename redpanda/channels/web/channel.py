from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import BinaryIO
from uuid import uuid4

from redpanda.assistant.attachments import (
    AttachmentGateway,
    AttachmentNotInSession,
    AttachmentRejected,
    is_valid_attachment_id,
)
from redpanda.assistant.file_attachments import FileAttachment, is_file_attachment_id
from redpanda.channels.web.directory_picker import select_file


class WebConnectionInactive(Exception):
    """浏览器连接已经失效，需要重新连接。"""


class WebInputInvalid(Exception):
    """Web 输入不符合已知的用户输入约束。"""


@dataclass(frozen=True, slots=True)
class WebConnection:
    connection_id: str

    @property
    def owner(self) -> str:
        return f"web:{self.connection_id}"


class WebChannel:
    def __init__(
        self,
        sessions,
        queries,
        attachments: AttachmentGateway | None = None,
    ) -> None:
        self._sessions = sessions
        self._queries = queries
        self._attachments = attachments
        self._connections: set[str] = set()

    def connect(self) -> WebConnection:
        connection = WebConnection(f"connection-{uuid4().hex}")
        self._connections.add(connection.connection_id)
        self._sessions.note_web_connected()
        return connection

    async def disconnect(self, connection: WebConnection) -> None:
        self._connections.remove(connection.connection_id)
        await self._sessions.release(connection.owner)
        self._sessions.note_web_disconnected()

    async def list_agents(self):
        return await self._queries.list_agents()

    async def create_agent(self, connection_id: str, workspace_id: str | None, name: str):
        connection = self._require_connection(connection_id)
        record = await self._sessions.create_agent(name=name, workspace_id=workspace_id)
        await self._sessions.select(connection.owner, record.session_id)
        return await self._queries.agent_entry(record.session_id)

    async def list_inbox(self):
        return await self._queries.list_inbox()

    async def answer_absence(self, connection_id: str, session_id: str, action: str):
        self._require_connection(connection_id)
        if action not in {"continue", "stop"}:
            raise WebInputInvalid("action 只能是 continue 或 stop。")
        await self._sessions.answer_absence(session_id, action)

    async def list_groups(self):
        return [{"group_id": group.group_id,
                 "name": group.name, "members": group.members}
                for group in await self._sessions.agent_groups.list()]

    async def group(self, group_id: str):
        return await self._sessions.agent_groups.view(group_id)

    async def create_group(self, connection_id: str, name: str, members: list[str]):
        self._require_connection(connection_id)
        group = await self._sessions.agent_groups.create(name=name, members=members)
        return await self._sessions.agent_groups.view(group.group_id)

    async def post_group_message(self, connection_id: str, group_id: str, text: str, mention: list[str]):
        self._require_connection(connection_id)
        content = self._require_text(text)
        group = await self._sessions.agent_groups.get(group_id)
        if any(name not in group.members for name in mention):
            raise WebInputInvalid("只能点名这个群里的成员。")
        await self._sessions.agent_groups.post(group_id, content, mention)
        return await self._sessions.agent_groups.view(group_id)

    def routines(self, session_id: str):
        return [{
            "routine_id": routine.routine_id, "name": routine.name, "intent": routine.intent,
            "schedule": routine.schedule, "state": routine.state,
            "next_run": None if routine.next_due is None else routine.next_due.isoformat(),
            "last_run": None if routine.last_fired_at is None else routine.last_fired_at.isoformat(),
        } for routine in self._sessions.list_routines(session_id)]

    async def commands(self, session_id: str):
        return await self._sessions.list_commands(session_id)

    async def stop_command(self, connection_id: str, session_id: str, command_id: str):
        self._require_connection(connection_id)
        result = await self._sessions.stop_command(session_id, command_id)
        if result is None:
            raise WebInputInvalid("本会话没有该命令。")
        return result

    async def set_routine_state(self, connection_id: str, session_id: str, routine_id: str, state: str):
        self._require_connection(connection_id)
        await self._sessions.set_routine_state_by_user(session_id, routine_id, state)
        return self.routines(session_id)

    async def conversation(self, session_id: str):
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        return await self._queries.conversation(session_id)

    async def observe_subagent(self, parent_session_id: str, command_id: str):
        return await self._queries.observe_subagent(parent_session_id, command_id)

    async def file_changes(self, session_id: str, *, path=None, offset=0):
        return await self._queries.file_changes(session_id, path=path, offset=offset)

    async def select(self, connection_id: str, session_id: str):
        connection = self._require_connection(connection_id)
        view = await self._sessions.select(connection.owner, session_id)
        return await self._queries.conversation(session_id, view=view)

    async def save_image(
        self,
        connection_id: str,
        session_id: str,
        data: bytes,
        mime: str,
    ):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if type(data) is not bytes:
            raise TypeError("image data must be bytes")
        if type(mime) is not str or not mime:
            raise AttachmentRejected("缺少图片 MIME")
        await self.conversation(session_id)
        return self._store().for_session(session_id).save_image(data, mime)

    async def save_file_stream(
        self, connection_id: str, session_id: str, stream: BinaryIO, name: str,
    ) -> FileAttachment:
        self._require_connection(connection_id)
        await self.conversation(session_id)
        return self._store().for_session(session_id).save_file_stream(stream, name)

    async def save_file_upload(
        self, connection_id: str, session_id: str, read, name: str,
    ) -> FileAttachment:
        self._require_connection(connection_id)
        await self.conversation(session_id)
        return await self._store().for_session(session_id).save_file_upload(read, name)

    async def select_local_file(
        self, connection_id: str, session_id: str,
    ) -> FileAttachment | None:
        self._require_connection(connection_id)
        await self.conversation(session_id)
        source = await select_file()
        if source is None:
            return None
        self._require_connection(connection_id)
        return await asyncio.to_thread(
            self._store().for_session(session_id).save_file_path, source,
        )

    async def attachment_file(self, session_id: str, attachment_id: str):
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if not is_valid_attachment_id(attachment_id):
            raise AttachmentRejected("attachment id 格式无效")
        await self.conversation(session_id)
        if not await self._store().available_in_session(session_id, attachment_id):
            raise AttachmentNotInSession("附件不属于当前会话。")
        store = self._store().for_session(session_id)
        path = store.path(attachment_id)
        if not path.is_file():
            raise FileNotFoundError(path)
        mime = (
            "application/octet-stream" if is_file_attachment_id(attachment_id)
            else store.inspect(attachment_id).mime
        )
        return path, mime

    async def accept_input(
        self,
        connection_id: str,
        session_id: str,
        text: str,
        delivery_id: str,
        artifact_refs: tuple[str, ...] = (),
    ):
        self._require_connection(connection_id)
        content = self._require_text(text)
        if type(delivery_id) is not str or not delivery_id:
            raise ValueError("delivery_id must be a non-empty str")
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        refs = await self._require_refs(session_id, artifact_refs)
        view = await self._sessions.accept_input(
            session_id,
            content,
            delivery_id=delivery_id,
            source="web",
            **({"artifact_refs": refs} if refs else {}),
        )
        return await self._queries.conversation(session_id, view=view)

    async def cancel(self, connection_id: str, session_id: str):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        view = await self._sessions.cancel_turn(session_id)
        return await self._queries.conversation(session_id, view=view)

    async def retry(self, connection_id: str, session_id: str):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        view = await self._sessions.retry(session_id)
        return await self._queries.conversation(session_id, view=view)

    async def authorize_command(
        self,
        connection_id: str,
        session_id: str,
        command_id: str,
        approved: bool,
    ):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if type(command_id) is not str or not command_id:
            raise ValueError("command_id must be a non-empty str")
        await self._sessions.resolve_authorization(
            session_id,
            command_id,
            approved=approved,
        )
        view = await self._sessions.view(session_id)
        return await self._queries.conversation(session_id, view=view)

    async def resolve_control(
        self,
        connection_id: str,
        session_id: str,
        request_id: str,
        approved: bool,
    ):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        await self._sessions.resolve_control(
            session_id,
            request_id,
            approved=approved,
        )
        view = await self._sessions.view(session_id)
        return await self._queries.conversation(session_id, view=view)

    async def set_auto_authorize(
        self,
        connection_id: str,
        session_id: str,
        enabled: bool,
    ):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        view = await self._sessions.set_auto_authorize(
            session_id,
            bool(enabled),
        )
        return await self._queries.conversation(session_id, view=view)

    def model_selection(self, session_id: str):
        return self._sessions.model_selection(session_id)

    def set_model(self, connection_id: str, session_id: str, model: str):
        self._require_connection(connection_id)
        return self._sessions.set_model(session_id, model)

    def _store(self) -> AttachmentGateway:
        if self._attachments is None:
            raise RuntimeError("WebChannel 未装配附件网关")
        return self._attachments

    async def _require_refs(
        self, session_id: str, artifact_refs: tuple[str, ...]
    ) -> tuple[str, ...]:
        if type(artifact_refs) is not tuple:
            raise TypeError("artifact_refs must be tuple")
        if not artifact_refs:
            return ()
        store = self._store().for_session(session_id)
        for ref in artifact_refs:
            if not is_valid_attachment_id(ref):
                raise AttachmentRejected("artifact_refs 必须是有效附件 id")
            if not await self._store().available_in_session(session_id, ref):
                raise AttachmentRejected("附件不属于当前会话")
            path = store.path(ref)
            if not path.is_file():
                raise FileNotFoundError(path)
        return artifact_refs

    def _require_connection(self, connection_id: str) -> WebConnection:
        if type(connection_id) is not str or not connection_id:
            raise WebConnectionInactive("Web 连接无效，请重新连接页面。")
        if connection_id not in self._connections:
            raise WebConnectionInactive("Web 连接已失效，请重新连接页面。")
        return WebConnection(connection_id)

    @staticmethod
    def _require_text(text: str) -> str:
        if type(text) is not str:
            raise TypeError("text must be str")
        content = text.strip()
        if not content:
            raise WebInputInvalid("消息内容不能为空。")
        return content
