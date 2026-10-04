from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from redpanda.assistant.attachments import (
    AttachmentGateway,
    AttachmentRejected,
    MAX_SOURCE_BYTES,
    is_valid_attachment_id,
)
from redpanda.assistant.host.session_store import (
    ForkMessageNotFoundError,
    SessionForkUnavailableError,
)
from redpanda.assistant.file_attachments import is_file_attachment_id
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.model_connection import check_model_connection
from redpanda.model_settings import ModelConfigurationError, ModelInUseError
from redpanda.assistant.runner import SessionNotFoundError
from redpanda.assistant.workspace_versions import StepNotRewindable
from redpanda.bootstrap import bootstrap_assistant
from redpanda.channels.web.channel import WebChannel
from redpanda.channels.web.directory_picker import (
    DirectoryPickerUnavailable,
    select_directory,
    select_file,
)
from redpanda.channels.web.hub import WebEventHub
from redpanda.sandbox.registry import (
    WorkspaceNotFound,
    WorkspacePathTaken,
    WorkspaceRegistryError,
)

SSE_KEEPALIVE_SECONDS = 15


async def report_worker_failures(host: HostSupervisor, events: WebEventHub) -> None:
    """Worker 进程失败不是 Command outcome，Host 只把它放进队列等人来取。"""

    while True:
        failure = await host.wait_failure()
        if host.compact.store.reader_job(failure.session_id) is not None:
            continue
        await events.session_failed(
            failure.session_id,
            f"Session 进程失败：{failure.failure.render()}",
        )


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str


class ModelSettingsRequest(ConnectionRequest):
    config: dict


class SessionModelRequest(ConnectionRequest):
    model: str


class ModelTestRequest(ConnectionRequest):
    model: str


class CreateSessionRequest(ConnectionRequest):
    workspace_id: str


class InputRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str
    delivery_id: str
    text: str
    artifact_refs: list[str] = Field(default_factory=list)

    @field_validator("artifact_refs")
    @classmethod
    def attachment_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if not is_valid_attachment_id(item):
                raise ValueError("artifact_refs 必须是有效的附件 id")
        return value


class EditRequest(InputRequest):
    artifact_refs: list[str]
    message_id: str
    listed: bool = False
    restore_files: bool = False


class RestartRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str
    delivery_id: str
    step_id: str


class BranchRequest(ConnectionRequest):
    message_id: str


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str
    approved: bool


class ControlDecisionRequest(AuthorizationRequest):
    request_id: str


class AutoAuthorizeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str
    enabled: bool


class PauseRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str
    paused: bool


class TitleRequest(ConnectionRequest):
    title: str

    @field_validator("title")
    @classmethod
    def named(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must be a non-empty str")
        return stripped


class WorkspaceCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    name: str
    task_root: str
    full_access: bool = False


class AttachLocalFileRequest(ConnectionRequest):
    path: str


def create_web_app(
    channel: WebChannel | None = None,
    hub: WebEventHub | None = None,
    workspace_path: Path | None = None,
    workspaces=None,
    models=None,
    model_llm=None,
) -> FastAPI:
    events = hub if hub is not None else WebEventHub()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.hub = events
        if channel is not None:
            app.state.channel = channel
            app.state.models = models
            app.state.model_llm = model_llm
            app.state.workspaces = workspaces
            yield
            return
        async with bootstrap_assistant(
            events.output_final,
            workspace_path=workspace_path,
            preview_sink=events.preview,
            thinking_sink=events.thinking,
            subagent_output_sink=events.output_final,
            session_activity_sink=events.session_activity,
            session_failed_sink=events.session_failed,
            schedule_changed_sink=events.schedule_changed,
            tool_progress_sink=events.tool_progress,
            authorization_required_sink=events.authorization_required,
            context_usage_sink=events.context_usage,
            conversation_status_sink=events.conversation_status,
        ) as assistant:
            app.state.channel = WebChannel(
                assistant.sessions,
                assistant.queries,
                AttachmentGateway(assistant.sessions_root),
            )
            app.state.models = assistant.sessions.models
            app.state.model_llm = assistant.sessions.llm
            app.state.workspaces = assistant.workspaces
            failures = asyncio.create_task(
                report_worker_failures(assistant.sessions, events),
                name="web-assistant-failure",
            )
            try:
                yield
            finally:
                failures.cancel()
                await asyncio.gather(failures, return_exceptions=True)

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(ModelConfigurationError)
    async def invalid_model_configuration(_request: Request, error: ModelConfigurationError):
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.exception_handler(ModelInUseError)
    async def model_in_use(_request: Request, error: ModelInUseError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(SessionNotFoundError)
    async def session_not_found(_request: Request, error: SessionNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ForkMessageNotFoundError)
    async def fork_message_not_found(
        _request: Request, error: ForkMessageNotFoundError
    ):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(SessionForkUnavailableError)
    async def fork_unavailable(
        _request: Request, error: SessionForkUnavailableError
    ):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(StepNotRewindable)
    async def step_not_rewindable(_request: Request, error: StepNotRewindable):
        return JSONResponse(
            status_code=409,
            content={"detail": f"这一步没有成功的版本记录，无法从它之后重开：{error}"},
        )

    @app.exception_handler(AttachmentRejected)
    async def attachment_rejected(_request: Request, error: AttachmentRejected):
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.exception_handler(WorkspaceNotFound)
    async def workspace_not_found(_request: Request, error: WorkspaceNotFound):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(WorkspacePathTaken)
    async def workspace_path_taken(_request: Request, error: WorkspacePathTaken):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(WorkspaceRegistryError)
    async def workspace_registry_error(
        _request: Request, error: WorkspaceRegistryError
    ):
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.exception_handler(DirectoryPickerUnavailable)
    async def directory_picker_unavailable(
        _request: Request, error: DirectoryPickerUnavailable
    ):
        return JSONResponse(status_code=503, content={"detail": str(error)})

    @app.get("/api/events", response_class=EventSourceResponse)
    async def stream_events(request: Request):
        web = _channel(request)
        connection = web.connect()
        queue = _hub(request).subscribe()
        try:
            yield ServerSentEvent(
                event="connected",
                data={"connection_id": connection.connection_id},
            )
            while not await request.is_disconnected():
                try:
                    item = await asyncio.wait_for(
                        queue.get(),
                        timeout=SSE_KEEPALIVE_SECONDS,
                    )
                except TimeoutError:
                    yield ServerSentEvent(comment="keep-alive")
                    continue
                yield ServerSentEvent(event=item.name, data=item.data)
        finally:
            _hub(request).unsubscribe(queue)
            await web.disconnect(connection)

    @app.get("/api/model-settings")
    async def model_settings(request: Request):
        return request.app.state.models.view()

    @app.put("/api/model-settings")
    async def save_model_settings(body: ModelSettingsRequest, request: Request):
        _channel(request)._require_connection(body.connection_id)
        return request.app.state.models.save(body.config)

    @app.post("/api/model-settings/test")
    async def test_model_connection(body: ModelTestRequest, request: Request):
        _channel(request)._require_connection(body.connection_id)
        request.app.state.models.require_candidate(body.model)
        return await check_model_connection(request.app.state.model_llm, body.model)

    @app.get("/api/sessions/{session_id}/model")
    async def session_model(session_id: str, request: Request):
        return _channel(request).model_selection(session_id)

    @app.put("/api/sessions/{session_id}/model")
    async def set_session_model(session_id: str, body: SessionModelRequest, request: Request):
        return _channel(request).set_model(body.connection_id, session_id, body.model)

    @app.get("/api/sessions")
    async def sessions(request: Request):
        return await _channel(request).list_sessions()

    @app.get("/api/session-titles")
    async def session_titles(request: Request):
        return _channel(request).session_titles()

    @app.get("/api/sessions/{session_id}")
    async def conversation(session_id: str, request: Request):
        return await _channel(request).conversation(session_id)

    @app.post("/api/sessions", status_code=201)
    async def create_session(body: CreateSessionRequest, request: Request):
        return await _channel(request).create(body.connection_id, body.workspace_id)

    @app.get("/api/sessions/{session_id}/subagents/{command_id}")
    async def observe_subagent(session_id: str, command_id: str, request: Request):
        return await _channel(request).observe_subagent(session_id, command_id)

    @app.get("/api/workspaces")
    async def list_workspaces(request: Request):
        registry = request.app.state.workspaces
        return [record.to_dict() for record in registry.workspaces]

    @app.post("/api/workspaces", status_code=201)
    async def create_workspace(body: WorkspaceCreateRequest, request: Request):
        registry = request.app.state.workspaces
        return registry.create(
            name=body.name,
            task_root=Path(body.task_root),
            full_access=body.full_access,
        ).to_dict()

    @app.post("/api/workspaces/select-directory")
    async def pick_workspace_directory():
        selected = await select_directory()
        return {
            "directory": None if selected is None else {
                "path": str(selected),
                "name": selected.name or str(selected),
            }
        }

    @app.post("/api/files/select")
    async def pick_local_file():
        selected = await select_file()
        return {
            "file": None if selected is None else {
                "path": str(selected),
                "name": selected.name or str(selected),
            }
        }

    @app.post("/api/sessions/{session_id}/select")
    async def select_session(
        session_id: str,
        body: ConnectionRequest,
        request: Request,
    ):
        return await _channel(request).select(body.connection_id, session_id)

    @app.post("/api/sessions/{session_id}/attachments", status_code=201)
    async def upload_attachment(
        session_id: str,
        request: Request,
        connection_id: str = Form(),
        file: UploadFile = File(),
    ):
        mime = (file.content_type or "").split(";", 1)[0].strip().lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if not file.filename:
            raise AttachmentRejected("缺少附件文件名")
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            ref = await _channel(request).save_file_upload(
                connection_id, session_id, file.read, file.filename,
            )
            return {"kind": "file", "attachment_id": ref.attachment_id, "name": ref.name, "size": ref.size}
        data = await file.read(MAX_SOURCE_BYTES + 1)
        ref = await _channel(request).save_image(
            connection_id,
            session_id,
            data,
            mime,
        )
        return {
            "kind": "image",
            "name": file.filename,
            "size": len(data),
            "attachment_id": ref.attachment_id,
            "mime": ref.mime,
            "width": ref.width,
            "height": ref.height,
        }

    @app.post("/api/sessions/{session_id}/attachments/from-path", status_code=201)
    async def attach_local_file(
        session_id: str,
        body: AttachLocalFileRequest,
        request: Request,
    ):
        ref = await _channel(request).save_file_path(
            body.connection_id, session_id, Path(body.path),
        )
        return {
            "kind": "file",
            "attachment_id": ref.attachment_id,
            "name": ref.name,
            "size": ref.size,
        }

    @app.get("/api/sessions/{session_id}/attachments/{attachment_id:path}")
    async def download_attachment(
        session_id: str,
        attachment_id: str,
        request: Request,
    ):
        try:
            path, mime = await _channel(request).attachment_file(
                session_id,
                attachment_id,
            )
        except FileNotFoundError as error:
            return JSONResponse(status_code=404, content={"detail": str(error)})
        return FileResponse(
            path, media_type=mime,
            filename=path.name if is_file_attachment_id(attachment_id) else None,
        )

    @app.post("/api/sessions/{session_id}/inputs")
    async def accept_input(session_id: str, body: InputRequest, request: Request):
        return await _channel(request).accept_input(
            body.connection_id,
            session_id,
            body.text,
            body.delivery_id,
            tuple(body.artifact_refs),
        )

    @app.post("/api/sessions/{session_id}/control")
    async def resolve_control(
        session_id: str,
        body: ControlDecisionRequest,
        request: Request,
    ):
        return await _channel(request).resolve_control(
            body.connection_id,
            session_id,
            body.request_id,
            body.approved,
        )

    @app.post("/api/sessions/{session_id}/forks", status_code=201)
    async def edit_and_fork(
        session_id: str,
        body: EditRequest,
        request: Request,
    ):
        return await _channel(request).edit_and_fork(
            body.connection_id,
            session_id,
            body.message_id,
            body.text,
            body.delivery_id,
            tuple(body.artifact_refs),
            body.listed,
            body.restore_files,
        )

    @app.post("/api/sessions/{session_id}/branches", status_code=201)
    async def branch_after_turn(
        session_id: str,
        body: BranchRequest,
        request: Request,
    ):
        return await _channel(request).branch_after_turn(
            body.connection_id,
            session_id,
            body.message_id,
        )

    @app.post("/api/sessions/{session_id}/restarts", status_code=201)
    async def restart_from_step(
        session_id: str,
        body: RestartRequest,
        request: Request,
    ):
        return await _channel(request).restart_from_step(
            body.connection_id,
            session_id,
            body.step_id,
            body.delivery_id,
        )

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_session(
        session_id: str,
        body: ConnectionRequest,
        request: Request,
    ):
        return await _channel(request).cancel(body.connection_id, session_id)

    @app.post("/api/sessions/{session_id}/retry")
    async def retry_session(
        session_id: str,
        body: ConnectionRequest,
        request: Request,
    ):
        return await _channel(request).retry(body.connection_id, session_id)

    @app.post("/api/sessions/{session_id}/commands/{command_id}/authorize")
    async def authorize_command(
        session_id: str,
        command_id: str,
        body: AuthorizationRequest,
        request: Request,
    ):
        return await _channel(request).authorize_command(
            body.connection_id,
            session_id,
            command_id,
            body.approved,
        )

    @app.post("/api/sessions/{session_id}/auto-authorize")
    async def set_auto_authorize(
        session_id: str,
        body: AutoAuthorizeRequest,
        request: Request,
    ):
        return await _channel(request).set_auto_authorize(
            body.connection_id,
            session_id,
            body.enabled,
        )

    @app.post("/api/sessions/{session_id}/paused")
    async def set_paused(
        session_id: str,
        body: PauseRequest,
        request: Request,
    ):
        return await _channel(request).set_paused(
            body.connection_id,
            session_id,
            body.paused,
        )

    @app.post("/api/sessions/{session_id}/archive")
    async def archive_session(
        session_id: str,
        body: ConnectionRequest,
        request: Request,
    ):
        return await _channel(request).archive(body.connection_id, session_id)

    @app.post("/api/sessions/{session_id}/title")
    async def set_session_title(
        session_id: str,
        body: TitleRequest,
        request: Request,
    ):
        return await _channel(request).set_title(
            body.connection_id,
            session_id,
            body.title,
        )

    assets = Path(__file__).parents[3] / "web" / "dist"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets / "assets"), name="assets")

        @app.get("/{path:path}")
        async def frontend(path: str):
            candidate = assets / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(assets / "index.html")

    return app


def _channel(request: Request) -> WebChannel:
    return request.app.state.channel


def _hub(request: Request) -> WebEventHub:
    return request.app.state.hub
