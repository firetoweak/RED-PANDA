from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Literal

from anyio import CancelScope
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from redpanda.assistant.attachments import (
    AttachmentGateway,
    AttachmentNotInSession,
    AttachmentRejected,
    MAX_SOURCE_BYTES,
    is_valid_attachment_id,
)
from redpanda.assistant.file_attachments import is_file_attachment_id
from redpanda.assistant.conversations import FileChangesUnavailable
from redpanda.assistant.authorization import AuthorizationConflict, AuthorizationTargetNotFound
from redpanda.assistant.host.agent_groups import GroupNameTaken, GroupNotFound
from redpanda.assistant.host.agent_roster import AgentNameTaken, AgentNotFound
from redpanda.assistant.host.agent_projects import ProjectLinkRefused
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.model_connection import check_model_connection
from redpanda.automation.routines import RoutineNotFound
from redpanda.model_settings import ModelConfigurationError, ModelInUseError
from redpanda.assistant.runner import SessionNotFoundError
from redpanda.assistant.inputs import InputConflict
from redpanda.bootstrap import bootstrap_assistant
from redpanda.channels.web.channel import WebChannel, WebConnectionInactive, WebInputInvalid
from redpanda.channels.web.directory_picker import (
    DirectoryPickerUnavailable,
    select_directory,
)
from redpanda.channels.web.hub import WebEventHub
from redpanda.filesystem.registry import (
    WorkspaceNotFound,
    WorkspacePathTaken,
    WorkspaceRegistryError,
)

SSE_KEEPALIVE_SECONDS = 15


class EventStreamCancellation:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        try:
            await self.app(scope, receive, send)
        except asyncio.CancelledError:
            # Shutdown can cancel either the sender or the disconnect listener.
            if scope["type"] == "http" and scope["path"] == "/api/events":
                return
            raise


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

    connection_id: str = Field(min_length=1)


class AbsenceAnswer(ConnectionRequest):
    session_id: str = Field(min_length=1)
    action: Literal["continue", "stop"]


class ModelSettingsRequest(ConnectionRequest):
    config: dict


class SessionModelRequest(ConnectionRequest):
    model: str


class ModelTestRequest(ConnectionRequest):
    model: str


class CreateAgentRequest(ConnectionRequest):
    workspace_id: str | None = Field(default=None, min_length=1)
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def stable_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Agent 名字不能以空白开头或结尾")
        return value


class LinkProjectRequest(ConnectionRequest):
    """A registered project by id, or a local Git repository registered in place."""
    workspace_id: str | None = Field(default=None, min_length=1)
    path: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def one_source(self):
        if (self.workspace_id is None) == (self.path is None):
            raise ValueError("workspace_id 和 path 只给一个")
        return self


class CreateGroupRequest(ConnectionRequest):
    name: str = Field(min_length=1)
    members: list[str] = Field(min_length=2, max_length=6)

    @field_validator("name")
    @classmethod
    def stable_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("群名不能以空白开头或结尾")
        return value

    @field_validator("members")
    @classmethod
    def distinct(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("群成员不能重复")
        return value


class GroupMessageRequest(ConnectionRequest):
    text: str
    mention: list[str] = Field(default_factory=list)


class RoutineStateRequest(ConnectionRequest):
    state: Literal["active", "paused_by_user", "deleted"]


class InputRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str = Field(min_length=1)
    delivery_id: str = Field(min_length=1)
    text: str
    artifact_refs: list[str] = Field(default_factory=list)

    @field_validator("artifact_refs")
    @classmethod
    def attachment_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            if not is_valid_attachment_id(item):
                raise ValueError("artifact_refs 必须是有效的附件 id")
        return value


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str = Field(min_length=1)
    approved: bool


class ControlDecisionRequest(AuthorizationRequest):
    request_id: str = Field(min_length=1)


class AutoAuthorizeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str = Field(min_length=1)
    enabled: bool


class WorkspaceCreateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    name: str
    task_root: str
    full_access: bool = False


class WorkspaceRenameRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def named(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must be a non-empty str")
        return stripped


def create_web_app(
    channel: WebChannel | None = None,
    hub: WebEventHub | None = None,
    workspace_path: Path | None = None,
    workspaces=None,
    models=None,
    model_llm=None,
    static_dir: Path | None = None,
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
            group_changed_sink=events.group_changed,
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
    app.add_middleware(EventStreamCancellation)

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, _error: Exception):
        # Starlette still re-raises to the server logger, preserving the diagnostic traceback.
        return JSONResponse(status_code=500, content={
            "detail": "服务处理请求时发生内部错误，请稍后重试；若持续失败，请查看服务日志。",
        })

    @app.exception_handler(WebConnectionInactive)
    async def inactive_connection(_request: Request, error: WebConnectionInactive):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(WebInputInvalid)
    async def invalid_input(_request: Request, error: WebInputInvalid):
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.exception_handler(InputConflict)
    async def input_conflict(_request: Request, error: InputConflict):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(AuthorizationTargetNotFound)
    async def authorization_missing(_request: Request, error: AuthorizationTargetNotFound):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(AuthorizationConflict)
    async def authorization_conflict(_request: Request, error: AuthorizationConflict):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(ModelConfigurationError)
    async def invalid_model_configuration(_request: Request, error: ModelConfigurationError):
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.exception_handler(ModelInUseError)
    async def model_in_use(_request: Request, error: ModelInUseError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(SessionNotFoundError)
    async def session_not_found(_request: Request, error: SessionNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(AttachmentRejected)
    async def attachment_rejected(_request: Request, error: AttachmentRejected):
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.exception_handler(WorkspaceNotFound)
    async def workspace_not_found(_request: Request, error: WorkspaceNotFound):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(AgentNameTaken)
    async def agent_name_taken(_request: Request, error: AgentNameTaken):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(GroupNameTaken)
    async def group_name_taken(_request: Request, error: GroupNameTaken):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(GroupNotFound)
    async def group_not_found(_request: Request, _error: GroupNotFound):
        return JSONResponse(status_code=404, content={"detail": "群不存在"})

    @app.exception_handler(RoutineNotFound)
    async def routine_not_found(_request: Request, _error: RoutineNotFound):
        return JSONResponse(status_code=404, content={"detail": "这个 Agent 没有这项例行任务，可能已被删除。"})

    @app.exception_handler(AgentNotFound)
    async def agent_not_found(_request: Request, error: AgentNotFound):
        return JSONResponse(status_code=404, content={"detail": f"同项目中没有这位 Agent：{error}"})

    @app.exception_handler(ProjectLinkRefused)
    async def project_link_refused(_request: Request, error: ProjectLinkRefused):
        return JSONResponse(status_code=409, content={"detail": error.error, "code": error.code, "data": error.data})

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
            # SSE disconnect cancels its task group; finish releasing the Worker.
            with CancelScope(shield=True):
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
        profile = request.app.state.models.require_candidate(body.model)
        return await check_model_connection(request.app.state.model_llm, body.model,
                                            reasoning_effort=profile.reasoning_effort)

    @app.get("/api/sessions/{session_id}/model")
    async def session_model(session_id: str, request: Request):
        return _channel(request).model_selection(session_id)

    @app.put("/api/sessions/{session_id}/model")
    async def set_session_model(session_id: str, body: SessionModelRequest, request: Request):
        return _channel(request).set_model(body.connection_id, session_id, body.model)

    @app.get("/api/agents")
    async def agents(request: Request):
        return await _channel(request).list_agents()

    @app.post("/api/agents", status_code=201)
    async def create_agent(body: CreateAgentRequest, request: Request):
        return await _channel(request).create_agent(
            body.connection_id, body.workspace_id, body.name,
        )

    @app.post("/api/agents/{session_id}/projects")
    async def link_project(session_id: str, body: LinkProjectRequest, request: Request):
        return await _channel(request).link_project(body.connection_id, session_id, body.workspace_id, body.path)

    @app.post("/api/agents/{session_id}/projects/{name}/unlink")
    async def unlink_project(session_id: str, name: str, body: ConnectionRequest, request: Request):
        return await _channel(request).unlink_project(body.connection_id, session_id, name)

    @app.get("/api/groups")
    async def groups(request: Request):
        return await _channel(request).list_groups()

    @app.post("/api/groups", status_code=201)
    async def create_group(body: CreateGroupRequest, request: Request):
        return await _channel(request).create_group(body.connection_id, body.name, body.members)

    @app.get("/api/groups/{group_id}")
    async def group(group_id: str, request: Request):
        return await _channel(request).group(group_id)

    @app.post("/api/groups/{group_id}/messages")
    async def post_group_message(group_id: str, body: GroupMessageRequest, request: Request):
        return await _channel(request).post_group_message(body.connection_id, group_id, body.text, body.mention)

    @app.get("/api/inbox")
    async def inbox(request: Request):
        return await _channel(request).list_inbox()

    @app.post("/api/inbox/absence")
    async def answer_absence(body: AbsenceAnswer, request: Request):
        await _channel(request).answer_absence(body.connection_id, body.session_id, body.action)
        return {"ok": True}

    @app.get("/api/sessions/{session_id}")
    async def conversation(session_id: str, request: Request):
        return await _channel(request).conversation(session_id)

    @app.get("/api/sessions/{session_id}/routines")
    async def routines(session_id: str, request: Request):
        return _channel(request).routines(session_id)

    @app.get("/api/sessions/{session_id}/routines/{routine_id}/fires")
    async def routine_fires(session_id: str, routine_id: str, request: Request):
        return await _channel(request).routine_fires(session_id, routine_id)

    @app.get("/api/sessions/{session_id}/commands")
    async def commands(session_id: str, request: Request):
        return await _channel(request).commands(session_id)

    @app.post("/api/sessions/{session_id}/commands/{command_id}/stop")
    async def stop_command(session_id: str, command_id: str, body: ConnectionRequest, request: Request):
        return await _channel(request).stop_command(body.connection_id, session_id, command_id)

    @app.put("/api/sessions/{session_id}/routines/{routine_id}/state")
    async def set_routine_state(session_id: str, routine_id: str, body: RoutineStateRequest, request: Request):
        return await _channel(request).set_routine_state(body.connection_id, session_id, routine_id, body.state)

    @app.get("/api/sessions/{session_id}/changes")
    async def file_changes(session_id: str, request: Request, project: str | None = None, path: str | None = None,
                           revision: str | None = None, offset: int = Query(default=0, ge=0)):
        if path is not None:
            logical = PurePosixPath(path)
            if (not path or logical.is_absolute() or logical.as_posix() != path
                    or ".." in logical.parts or path == "." or any(char in path for char in "\\:\0")):
                raise HTTPException(422, "文件路径必须是工作区内的相对路径。")
        elif offset or revision is not None:
            raise HTTPException(422, "续读时必须指定文件。")
        try:
            result = await _channel(request).file_changes(session_id, project=project, path=path, offset=offset)
        except FileChangesUnavailable as error:
            raise HTTPException(503, str(error)) from error
        if result is None:
            raise HTTPException(404, "该文件已没有当前会话的变更，请刷新列表。")
        if revision is not None and result["revision"] != revision:
            raise HTTPException(409, "文件变化已更新，请刷新后查看。")
        return result

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

    @app.patch("/api/workspaces/{workspace_id}")
    async def rename_workspace(
        workspace_id: str, body: WorkspaceRenameRequest, request: Request,
    ):
        return request.app.state.workspaces.rename(workspace_id, body.name).to_dict()

    @app.delete("/api/workspaces/{workspace_id}", status_code=204)
    async def delete_workspace(workspace_id: str, request: Request):
        request.app.state.workspaces.delete(workspace_id)

    @app.post("/api/workspaces/select-directory")
    async def pick_workspace_directory():
        selected = await select_directory()
        return {
            "directory": None if selected is None else {
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

    @app.post("/api/sessions/{session_id}/attachments/select")
    async def attach_local_file(
        session_id: str,
        body: ConnectionRequest,
        request: Request,
    ):
        ref = await _channel(request).select_local_file(
            body.connection_id, session_id,
        )
        return {
            "file": None if ref is None else {
                "kind": "file",
                "attachment_id": ref.attachment_id,
                "name": ref.name,
                "size": ref.size,
            },
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
        except AttachmentNotInSession as error:
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

    @app.get("/api/auto-authorize")
    def auto_authorize(request: Request):
        return _channel(request).auto_authorize()

    @app.post("/api/auto-authorize")
    async def set_auto_authorize(
        body: AutoAuthorizeRequest,
        request: Request,
    ):
        try:
            return await _channel(request).set_auto_authorize(
                body.connection_id,
                body.enabled,
            )
        finally:
            await request.app.state.hub.auto_authorize_changed()

    if static_dir is not None:
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")
        app.mount("/", _FrontendFiles(directory=static_dir), name="frontend")

    return app


class _FrontendFiles(StaticFiles):
    """前端构建产物；未知页面路径交给前端路由，未知 API 路径保持 404。"""

    async def get_response(self, path: str, scope) -> Response:
        if Path(path).parts[:1] == ("api",):
            raise StarletteHTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
        return await super().get_response("index.html", scope)


def _channel(request: Request) -> WebChannel:
    return request.app.state.channel


def _hub(request: Request) -> WebEventHub:
    return request.app.state.hub
