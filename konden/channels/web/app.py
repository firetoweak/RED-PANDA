from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from anyio import CancelScope
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from konden.assistant.attachments import (
    AttachmentGateway,
    AttachmentNotInSession,
    AttachmentRejected,
    MAX_SOURCE_BYTES,
    is_valid_attachment_id,
)
from konden.assistant.file_attachments import is_file_attachment_id
from konden.assistant.authorization import AuthorizationConflict, AuthorizationTargetNotFound
from konden.assistant.host.agent_groups import GroupDissolvedError, GroupNameTaken, GroupNotFound
from konden.assistant.host.agent_roster import AgentDeletedError, AgentNameTaken, AgentNotFound
from konden.assistant.host.agent_projects import ProjectLinkRefused
from konden.assistant.host.external_messages import ExternalConnectionNotFound, ExternalSessionNotFound
from konden.assistant.host.supervisor import HostSupervisor
from konden.assistant.model_connection import check_model_connection
from konden.assistant.shared_memory import SharedMemoryStore, UpdateSharedMemory
from konden.automation.routines import RoutineNotFound
from konden.model_settings import ModelCatalogUnavailable, ModelConfigurationError, ModelInUseError
from konden.assistant.runner import SessionNotFoundError
from konden.assistant.inputs import InputConflict
from konden.bootstrap import bootstrap_assistant
from konden.channels.web.channel import WebChannel, WebConnectionInactive, WebInputInvalid
from konden.channels.web.avatars import AvatarImageNotFound, AvatarStore
from konden.channels.web.directory_picker import (
    DirectoryPickerUnavailable,
    select_directory,
)
from konden.channels.web.hub import WebEventHub
from konden.channels.web.capabilities import CapabilityViews
from konden.cli.registry import CliRegistry
from konden.skills.errors import SkillInputError, SkillInstalledPackageError, SkillNotFoundError
from konden.mcp.errors import McpServerNotFoundError
from konden.channels.mcp import create_mcp_app
from konden.filesystem.registry import (
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


class SharedMemoryRequest(ConnectionRequest, UpdateSharedMemory):
    delivery_id: str = Field(min_length=1)


class SessionModelRequest(ConnectionRequest):
    model: str


class ModelTestRequest(ConnectionRequest):
    model: str


class AgentNameRequest(ConnectionRequest):
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def stable_name(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("Agent 名字不能以空白开头或结尾")
        return value


class AgentAvatarRequest(ConnectionRequest):
    seed: str | None = Field(min_length=1)


class CreateAgentRequest(AgentNameRequest):
    path: str | None = Field(default=None, min_length=1)


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
    delivery_id: str = Field(min_length=1)
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
    interrupt: bool = False

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


class CapabilityEnabledRequest(ConnectionRequest):
    enabled: bool


def create_web_app(
    channel: WebChannel | None = None,
    hub: WebEventHub | None = None,
    workspace_path: Path | None = None,
    workspaces=None,
    models=None,
    model_llm=None,
    static_dir: Path | None = None,
    shared_memory: SharedMemoryStore | None = None,
    avatars: AvatarStore | None = None,
    external_messages=None,
    capabilities: CapabilityViews | None = None,
) -> FastAPI:
    events = hub if hub is not None else WebEventHub()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mcp_app = create_mcp_app(lambda: app.state.external_messages)
        app.state.collaboration_mcp = mcp_app
        app.state.hub = events
        app.state.external_messages = external_messages
        if external_messages is not None:
            external_messages.changed_sink = events.external_sessions_changed
        if channel is not None:
            app.state.channel = channel
            app.state.models = models
            app.state.model_llm = model_llm
            app.state.workspaces = workspaces
            app.state.shared_memory = shared_memory
            app.state.avatars = avatars
            app.state.capabilities = capabilities
            async with mcp_app.router.lifespan_context(mcp_app):
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
            app.state.external_messages = assistant.sessions.external_messages
            app.state.external_messages.changed_sink = events.external_sessions_changed
            app.state.channel = WebChannel(
                assistant.sessions,
                assistant.queries,
                AttachmentGateway(assistant.sessions_root),
            )
            app.state.models = assistant.sessions.models
            app.state.model_llm = assistant.sessions.llm
            app.state.workspaces = assistant.workspaces
            app.state.shared_memory = SharedMemoryStore(assistant.sessions.home.state_root / "shared_memory.sqlite")
            app.state.avatars = avatars if avatars is not None else AvatarStore(assistant.sessions.home.state_root / "web_avatars")
            app.state.capabilities = CapabilityViews(
                assistant.skill_service, assistant.mcp_service,
                CliRegistry(assistant.sessions.home.clis_root),
            )
            failures = asyncio.create_task(
                report_worker_failures(assistant.sessions, events),
                name="web-assistant-failure",
            )
            try:
                async with mcp_app.router.lifespan_context(mcp_app):
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

    @app.exception_handler(ModelCatalogUnavailable)
    async def unavailable_model_catalog(_request: Request, error: ModelCatalogUnavailable):
        return JSONResponse(status_code=502, content={"detail": str(error)})

    @app.exception_handler(SessionNotFoundError)
    async def session_not_found(_request: Request, error: SessionNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(AvatarImageNotFound)
    async def avatar_image_not_found(_request: Request, _error: AvatarImageNotFound):
        return JSONResponse(status_code=404, content={"detail": "头像图片不存在"})

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

    @app.get("/api/capabilities")
    async def list_capabilities(request: Request):
        return await request.app.state.capabilities.read()

    @app.put("/api/capabilities/{kind}/{capability_id}/enabled")
    async def set_capability_enabled(
        kind: Literal["skills", "mcp"], capability_id: str,
        body: CapabilityEnabledRequest, request: Request,
    ):
        _channel(request)._require_connection(body.connection_id)
        views = request.app.state.capabilities
        try:
            record = (
                await views.skills.set_enabled(capability_id, body.enabled)
                if kind == "skills"
                else await views.mcp.set_server_enabled(capability_id, body.enabled)
            )
        except (SkillNotFoundError, McpServerNotFoundError) as error:
            return JSONResponse(status_code=404, content={"detail": str(error)})
        except SkillInstalledPackageError:
            raise
        except SkillInputError as error:
            return JSONResponse(status_code=400, content={"detail": str(error)})
        return {"enabled": record.enabled}

    @app.get("/api/model-settings")
    async def model_settings(request: Request):
        return request.app.state.models.view()

    @app.get("/api/model-settings/codex-models")
    async def codex_models(request: Request):
        return await request.app.state.models.codex_models()

    @app.get("/api/shared-memory")
    async def read_shared_memory(request: Request):
        return request.app.state.shared_memory.read().model_dump()

    @app.put("/api/shared-memory")
    async def save_shared_memory(body: SharedMemoryRequest, request: Request):
        _channel(request)._require_connection(body.connection_id)
        result = request.app.state.shared_memory.update(
            body, author="用户", source="user", operation_id=body.delivery_id,
        )
        if not result["ok"]:
            return JSONResponse(status_code=409, content={"detail": result["error"], "current": result["data"]})
        return result["data"]

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

    @app.exception_handler(AgentDeletedError)
    async def agent_deleted(_request: Request, _error: AgentDeletedError):
        return JSONResponse(status_code=410, content={"detail": "这位 Bot 已删除，只能查看历史记录。"})

    @app.exception_handler(GroupDissolvedError)
    async def group_dissolved(_request: Request, _error: GroupDissolvedError):
        return JSONResponse(status_code=410, content={"detail": "这个群已解散，只能查看历史记录。"})

    @app.get("/api/avatars")
    async def avatar_selections(request: Request):
        return request.app.state.avatars.read()

    @app.put("/api/agents/{session_id}/avatar")
    async def set_agent_avatar(session_id: str, body: AgentAvatarRequest, request: Request):
        await _channel(request).require_avatar_edit(body.connection_id, session_id)
        selection = request.app.state.avatars.set_generated(session_id, body.seed)
        await request.app.state.hub.avatars_changed()
        return selection

    @app.post("/api/agents/{session_id}/avatar/image", status_code=201)
    async def upload_avatar_image(
        session_id: str,
        request: Request,
        connection_id: str = Form(),
        file: UploadFile = File(),
    ):
        await _channel(request).require_avatar_edit(connection_id, session_id)
        mime = (file.content_type or "").split(";", 1)[0].strip().lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise AttachmentRejected("头像只支持 PNG、JPEG、WebP 或 GIF 图片")
        data = await file.read(MAX_SOURCE_BYTES + 1)
        selection = request.app.state.avatars.save_image(session_id, data, mime)
        await request.app.state.hub.avatars_changed()
        return selection

    @app.get("/api/avatars/images/{attachment_id}")
    async def avatar_image(attachment_id: str, request: Request):
        path, mime = request.app.state.avatars.image_file(attachment_id)
        return FileResponse(path, media_type=mime)

    @app.get("/api/agents")
    async def agents(request: Request):
        return await _channel(request).list_agents()

    @app.get("/api/external-connections")
    async def external_connections(request: Request):
        return await _channel(request).list_external_connections()

    @app.get("/api/external-sessions")
    async def external_sessions(request: Request):
        return await _channel(request).list_external_sessions()

    @app.get("/api/external-sessions/{identity}")
    async def external_session(identity: str, request: Request):
        try:
            return await _channel(request).external_session(identity)
        except ExternalSessionNotFound:
            return JSONResponse(status_code=404, content={"detail": "没有这个外部会话。"})

    @app.post("/api/external-connections", status_code=201)
    async def create_external_connection(body: ConnectionRequest, request: Request):
        created = await _channel(request).create_external_connection(body.connection_id)
        return JSONResponse(status_code=201, content={**created, "url": str(request.base_url) + "mcp/"},
                            headers={"Cache-Control": "no-store"})

    @app.delete("/api/external-connections/{external_id}", status_code=204)
    async def revoke_external_connection(external_id: str, body: ConnectionRequest, request: Request):
        try:
            await _channel(request).revoke_external_connection(body.connection_id, external_id)
        except ExternalConnectionNotFound:
            return JSONResponse(status_code=404, content={"detail": "没有这份连接配置，可能已撤销。"})
        return Response(status_code=204)

    @app.post("/api/agents", status_code=201)
    async def create_agent(body: CreateAgentRequest, request: Request):
        return await _channel(request).create_agent(
            body.connection_id, body.path, body.name,
        )

    @app.delete("/api/agents/{session_id}", status_code=204)
    async def delete_agent(session_id: str, body: ConnectionRequest, request: Request):
        await _channel(request).delete_agent(body.connection_id, session_id)
        return Response(status_code=204)

    @app.put("/api/agents/{session_id}/name")
    async def rename_agent(session_id: str, body: AgentNameRequest, request: Request):
        return await _channel(request).rename_agent(body.connection_id, session_id, body.name)

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

    @app.delete("/api/groups/{group_id}", status_code=204)
    async def dissolve_group(group_id: str, body: ConnectionRequest, request: Request):
        await _channel(request).dissolve_group(body.connection_id, group_id)
        return Response(status_code=204)

    @app.get("/api/groups/{group_id}")
    async def group(group_id: str, request: Request):
        return await _channel(request).group(group_id)

    @app.post("/api/groups/{group_id}/messages")
    async def post_group_message(group_id: str, body: GroupMessageRequest, request: Request):
        return await _channel(request).post_group_message(body.connection_id, group_id, body.text, body.mention,
                                                         delivery_id=body.delivery_id)

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

    @app.get("/api/sessions/{session_id}/subagents/{command_id}")
    async def observe_subagent(session_id: str, command_id: str, request: Request):
        return await _channel(request).observe_subagent(session_id, command_id)

    @app.get("/api/workspaces")
    async def list_workspaces(request: Request):
        registry = request.app.state.workspaces
        return [record.to_dict() for record in registry.workspaces]

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
            interrupt=body.interrupt,
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

    async def collaboration(scope, receive, send):
        await app.state.collaboration_mcp(scope, receive, send)

    app.mount("/mcp", collaboration, name="collaboration")
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
