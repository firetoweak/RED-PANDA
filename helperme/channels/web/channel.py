from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from helperme.assistant.attachments import (
    AttachmentGateway,
    AttachmentRejected,
    is_valid_attachment_id,
)
from helperme.assistant.file_attachments import FileAttachment, is_file_attachment_id


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
        return connection

    async def disconnect(self, connection: WebConnection) -> None:
        self._connections.remove(connection.connection_id)
        await self._sessions.release(connection.owner)

    async def list_sessions(self):
        return await self._queries.list_sessions()

    async def conversation(self, session_id: str):
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        return await self._queries.conversation(session_id)

    async def create(self, connection_id: str, workspace_id: str):
        connection = self._require_connection(connection_id)
        session_id = f"session-{uuid4().hex}"
        await self._sessions.create(session_id, workspace_id)
        view = await self._sessions.select(connection.owner, session_id)
        return await self._queries.conversation(session_id, view=view)

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

    async def save_file_path(
        self, connection_id: str, session_id: str, source: Path,
    ) -> FileAttachment:
        self._require_connection(connection_id)
        await self.conversation(session_id)
        if not isinstance(source, Path) or not source.is_absolute():
            raise AttachmentRejected("必须是本机绝对路径")
        return await asyncio.to_thread(
            self._store().for_session(session_id).save_file_path, source,
        )

    async def attachment_file(self, session_id: str, attachment_id: str):
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if not is_valid_attachment_id(attachment_id):
            raise AttachmentRejected("attachment id 格式无效")
        await self.conversation(session_id)
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
        refs = self._require_refs(session_id, artifact_refs)
        view = await self._sessions.accept_input(
            session_id,
            content,
            delivery_id=delivery_id,
            source="web",
            **({"artifact_refs": refs} if refs else {}),
        )
        return await self._queries.conversation(session_id, view=view)

    async def edit_and_fork(
        self,
        connection_id: str,
        source_session_id: str,
        message_id: str,
        text: str,
        delivery_id: str,
        listed: bool = False,
        restore_files: bool = False,
    ):
        connection = self._require_connection(connection_id)
        content = self._require_text(text)
        for label, value in (
            ("source_session_id", source_session_id),
            ("message_id", message_id),
            ("delivery_id", delivery_id),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{label} must be a non-empty str")
        child_session_id = f"session-{uuid4().hex}"
        view = await self._sessions.fork_and_accept_input(
            connection.owner,
            source_session_id,
            message_id,
            content,
            child_session_id=child_session_id,
            delivery_id=delivery_id,
            source="web",
            listed=bool(listed),
            restore_files=bool(restore_files),
        )
        return await self._queries.conversation(child_session_id, view=view)

    async def branch_after_turn(
        self,
        connection_id: str,
        source_session_id: str,
        message_id: str,
    ):
        connection = self._require_connection(connection_id)
        for label, value in (
            ("source_session_id", source_session_id),
            ("message_id", message_id),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{label} must be a non-empty str")
        child_session_id = f"session-{uuid4().hex}"
        view = await self._sessions.branch_after_turn(
            connection.owner,
            source_session_id,
            message_id,
            child_session_id,
        )
        return await self._queries.conversation(child_session_id, view=view)

    async def cancel(self, connection_id: str, session_id: str):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        view = await self._sessions.cancel_turn(session_id)
        return await self._queries.conversation(session_id, view=view)

    async def restart_from_step(
        self,
        connection_id: str,
        session_id: str,
        step_id: str,
        delivery_id: str,
    ):
        connection = self._require_connection(connection_id)
        for label, value in (
            ("session_id", session_id),
            ("step_id", step_id),
            ("delivery_id", delivery_id),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{label} must be a non-empty str")
        child_session_id = f"session-{uuid4().hex}"
        view = await self._sessions.restart_from_step(
            connection.owner,
            session_id,
            step_id,
            child_session_id,
            delivery_id,
        )
        return await self._queries.conversation(child_session_id, view=view)

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

    async def set_paused(
        self,
        connection_id: str,
        session_id: str,
        paused: bool,
    ):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        view = await self._sessions.set_paused(session_id, bool(paused))
        return await self._queries.conversation(session_id, view=view)

    def session_titles(self) -> dict[str, str]:
        return self._sessions.session_titles()

    async def archive(self, connection_id: str, session_id: str):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        self._sessions.archive(session_id)
        return {}

    async def set_title(self, connection_id: str, session_id: str, title: str):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        cleaned = self._require_text(title)
        self._sessions.set_title(session_id, cleaned)
        return {"title": cleaned}

    def _store(self) -> AttachmentGateway:
        if self._attachments is None:
            raise RuntimeError("WebChannel 未装配附件网关")
        return self._attachments

    def _require_refs(
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
            try:
                exists = store.path(ref).is_file()
            except FileNotFoundError:
                exists = False
            if not exists:
                raise AttachmentRejected("附件不存在")
        return artifact_refs

    def _require_connection(self, connection_id: str) -> WebConnection:
        if type(connection_id) is not str or not connection_id:
            raise ValueError("connection_id must be a non-empty str")
        if connection_id not in self._connections:
            raise ValueError("Web connection is not active")
        return WebConnection(connection_id)

    @staticmethod
    def _require_text(text: str) -> str:
        if type(text) is not str:
            raise TypeError("text must be str")
        content = text.strip()
        if not content:
            raise ValueError("text must be a non-empty str")
        return content
