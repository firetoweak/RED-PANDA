from __future__ import annotations

from redpanda.assistant.shared_memory import SharedMemoryStore, UpdateSharedMemory

import asyncio
import json
import unittest
from datetime import datetime, timezone
from functools import partial
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import unquote
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

from redpanda.assistant.attachments import AttachmentGateway
from redpanda.assistant.authorization import AuthorizationConflict, AuthorizationTargetNotFound
from redpanda.assistant.host.ipc import ProcessFailure, WorkerFailed
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.automation.routines import RoutineClock, RoutineStore
from redpanda.assistant.agents import AgentCreated, AgentRecord
from redpanda.assistant.conversations import (
    AgentProjectView,
    AgentRosterEntry,
    AuthorizationInboxItem,
    ConversationView,
    ControlInboxItem,
    UserItem,
    project_agent_status,
)
from redpanda.assistant.host.agent_groups import GroupNameTaken, GroupNotFound, GroupRecord
from redpanda.assistant.agents import GroupCreated
from redpanda.assistant.host.agent_roster import AgentNameTaken, AgentNotFound
from redpanda.assistant.host.agent_projects import ProjectLinkRefused
from redpanda.assistant.runner import SessionNotFoundError
from redpanda.model_settings import ModelSettings
from redpanda.config import write_json
from redpanda.llm.api import LLMAuthenticationError, LLMCallResult, LLMResponse, LLMUsage
from redpanda.paths import RedPandaHome
from redpanda.assistant.inputs import InputConflict
from redpanda.assistant.sessions import SessionView
from redpanda.channels.web import app as web_app
from redpanda.channels.web.app import create_web_app, report_worker_failures
from redpanda.channels.web.channel import WebChannel
from redpanda.channels.web.directory_picker import DirectoryPickerUnavailable
from redpanda.channels.web.hub import WebEventHub
from redpanda.filesystem.registry import WorkspaceRegistry


MODEL_CONFIG = {"model": {"default": "deepseek/test-model", "candidates": [
    {"model": "deepseek/test-model", "compact_threshold_tokens": 64000},
]}}


def test_static_resources_are_explicit(tmp_path):
    assets = tmp_path / "dist"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("frontend", encoding="utf-8")
    (assets / "assets/app.js").write_text("script", encoding="utf-8")
    for directory in (None, assets):
        app = create_web_app(channel=SimpleNamespace(), static_dir=directory)
        with TestClient(app) as client:
            if directory is None:
                assert client.get("/").status_code == 404
                assert client.get("/assets/app.js").status_code == 404
            else:
                assert client.get("/conversation").text == "frontend"
                assert client.get("/assets/app.js").text == "script"
                missing_api = client.get("/api/missing")
                assert missing_api.status_code == 404
                assert missing_api.json() == {"detail": "Not Found"}
        assert "/api/files/select" not in {route.path for route in app.routes}


def test_frontend_files_stay_inside_static_dir(tmp_path):
    assets = tmp_path / "dist"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("frontend", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    app = create_web_app(channel=SimpleNamespace(), static_dir=assets)

    async def get(target: str) -> bytes:
        # 按 uvicorn 的做法从原始请求目标得到 scope，不经过 HTTP 客户端的路径规范化。
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": unquote(target),
            "raw_path": target.encode("ascii"),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
        }
        body = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                body.append(message.get("body", b""))

        await app(scope, receive, send)
        return b"".join(body)

    for target in ("/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt", "/..%5csecret.txt"):
        assert asyncio.run(get(target)) == b"frontend", target


class _Sessions:
    def __init__(self, queries):
        self.queries = queries
        self.calls = []
        self.control_message = None
        self.auto_authorize_enabled = True

    def model_selection(self, session_id):
        return self.models.selection(session_id)

    def set_model(self, session_id, model):
        return self.models.select(session_id, model)

    async def create_agent(self, *, name, workspace_id):
        self.calls.append(("create_agent", name, workspace_id))
        if name == "taken":
            raise AgentNameTaken(f"项目内已有 Agent：{name}")
        if name == "nogit":
            raise ProjectLinkRefused("PROJECT_UNUSABLE", "项目必须是 Git 仓库，请先初始化并创建首个提交。")
        self.queries.created = (name, workspace_id)
        return AgentRecord(
            "agent-new",
            AgentCreated(name=name, directory=str((Path.cwd() / "agent-worktree").resolve())),
        )

    async def select(self, owner, session_id):
        self.calls.append(("select", owner, session_id))
        if session_id == "session-missing":
            raise SessionNotFoundError(session_id)
        return SessionView("waiting", ("external_fact",), (), False)

    async def accept_input(self, session_id, content, **kwargs):
        self.calls.append(("accept_input", session_id, content, kwargs))
        self.queries.record(session_id, content)
        return SessionView("waiting", ("external_fact",), (), False)

    async def cancel_turn(self, session_id):
        self.calls.append(("cancel_turn", session_id))
        return SessionView("waiting", ("external_fact",), (), False)

    async def retry(self, session_id):
        self.calls.append(("retry", session_id))
        return SessionView("runnable", (), (), True)

    async def release(self, owner):
        self.calls.append(("release", owner))

    def note_web_connected(self):
        return None

    def note_web_disconnected(self):
        return None

    async def answer_absence(self, session_id, action):
        self.calls.append(("answer_absence", session_id, action))

    async def view(self, session_id):
        self.calls.append(("view", session_id))
        return SessionView(
            "waiting",
            ("external_fact",),
            (),
            False,
            control_message=self.control_message,
        )

    async def resolve_authorization(self, session_id, command_id, *, approved):
        self.calls.append(("resolve_authorization", session_id, command_id, approved))

    async def resolve_control(self, session_id, request_id, *, approved):
        self.calls.append(("resolve_control", session_id, request_id, approved))
        self.control_message = "MCP Server `demo` 安装、测试并启用成功。能力目录已更新，load_toolset 之后工具从下一个 Step 可见。"
        return self.control_message

    def auto_authorize(self):
        return self.auto_authorize_enabled

    async def set_auto_authorize(self, enabled):
        self.calls.append(("set_auto_authorize", enabled))
        self.auto_authorize_enabled = enabled


class _Queries:
    def __init__(self):
        self.accepted: list[tuple[str, str]] = []
        self.created: tuple[str, str] | None = None

    def record(self, session_id, content):
        self.accepted.append((session_id, content))

    async def list_agents(self):
        return ()

    async def agent_entry(self, session_id):
        name, workspace_id = self.created
        projects = () if workspace_id is None else (AgentProjectView("old", workspace_id, "C:/bots/old"),)
        return AgentRosterEntry(session_id, projects, name, "前端", "idle", "waiting")

    async def list_inbox(self):
        return (
            AuthorizationInboxItem(
                "authorization", "session-old", "workspace-old", "验证熊猫",
                "cmd-1", "apply_patch", {"path": "note.txt"},
            ),
            ControlInboxItem(
                "control", "session-old", "workspace-old", "验证熊猫",
                "req-1", "删除工作区", "high",
            ),
        )

    async def conversation(self, session_id, *, view=None):
        items = tuple(
            UserItem(
                "user",
                f"user-{index}",
                text,
                datetime(2026, 9, 15, tzinfo=timezone.utc),
            )
            for index, (item_session, text) in enumerate(self.accepted)
            if item_session == session_id
        )
        return ConversationView(
            session_id,
            "workspace-old",
            len(items),
            items,
            view or SessionView("waiting", ("external_fact",), (), False),
        )


class WebFirstSliceTest(unittest.TestCase):
    def setUp(self):
        self._directory = TemporaryDirectory()
        self.queries = _Queries()
        self.sessions = _Sessions(self.queries)
        home = RedPandaHome(Path(self._directory.name))
        write_json(home.config_path, MODEL_CONFIG)
        self.models = ModelSettings(home, home.runtime_sessions_root, path=home.config_path)
        self.models.initialize_session("session-old")
        self.sessions.models = self.models
        self.shared_memory = SharedMemoryStore(home.state_root / "shared_memory.sqlite")
        self.channel = WebChannel(
            self.sessions,
            self.queries,
            AttachmentGateway(Path(self._directory.name)),
        )
        self.hub = WebEventHub()
        self.connection = self.channel.connect()
        self.model_llm = SimpleNamespace(chat=AsyncMock(return_value=LLMCallResult(
            LLMResponse(content="OK"), LLMUsage(input_tokens=8, output_tokens=1),
        )))
        self.workspaces = WorkspaceRegistry.load(
            Path(self._directory.name) / "workspaces.json"
        )
        self.client = TestClient(
            create_web_app(self.channel, self.hub, workspaces=self.workspaces,
                           models=self.models, model_llm=self.model_llm, shared_memory=self.shared_memory)
        )
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._directory.cleanup()

    def test_user_and_bots_edit_the_same_memory_with_revision_conflicts(self):
        self.assertEqual(self.client.get("/api/shared-memory").json()["content"], "")
        body = {"connection_id": self.connection.connection_id, "delivery_id": "user-save",
                "version": 0, "content": "交接附验证依据。"}
        saved = self.client.put("/api/shared-memory", json=body)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["updated_by"], "用户")
        self.assertEqual(self.client.put("/api/shared-memory", json=body).json(), saved.json())
        bot = self.shared_memory.update(UpdateSharedMemory(version=1, content="共享资源先协调。"),
                                        author="Ada", source="bot:ada", operation_id="bot-save")
        self.assertEqual(self.client.get("/api/shared-memory").json(), bot["data"])
        stale = self.client.put("/api/shared-memory", json={**body, "delivery_id": "stale", "version": 1})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["current"], bot["data"])
        self.assertEqual(self.shared_memory.read().content, "共享资源先协调。")

    def test_memory_web_boundary_enforces_length_and_active_connection(self):
        body = {"connection_id": self.connection.connection_id, "delivery_id": "limit", "version": 0,
                "content": "🐼" * 500}
        self.assertEqual(self.client.put("/api/shared-memory", json=body).status_code, 200)
        too_long = self.client.put("/api/shared-memory", json={**body, "delivery_id": "long", "content": "字" * 501})
        self.assertEqual(too_long.status_code, 422)
        inactive = self.client.put("/api/shared-memory", json={**body, "connection_id": "gone", "content": ""})
        self.assertEqual(inactive.status_code, 409)
        self.assertEqual(self.shared_memory.read().version, 1)

    def test_auto_authorize_is_global_and_notifies_other_pages(self):
        queue = self.hub.subscribe()
        self.assertEqual(self.client.get("/api/auto-authorize").json(), {"enabled": True})
        response = self.client.post("/api/auto-authorize", json={
            "connection_id": self.connection.connection_id, "enabled": False,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": False})
        self.assertEqual(self.client.get("/api/auto-authorize").json(), {"enabled": False})
        self.assertIn(("set_auto_authorize", False), self.sessions.calls)
        self.assertEqual(queue.get_nowait().name, "auto_authorize_changed")
        self.hub.unsubscribe(queue)

    def test_global_auto_authorize_requires_active_connection_and_boolean(self):
        for payload, status in (
            ({"connection_id": "expired", "enabled": False}, 409),
            ({"connection_id": self.connection.connection_id, "enabled": "false"}, 422),
        ):
            self.assertEqual(self.client.post("/api/auto-authorize", json=payload).status_code, status)
        self.assertTrue(self.sessions.auto_authorize())

    def test_failed_global_sync_notifies_pages_and_exposes_the_failure(self):
        async def fail(enabled):
            self.sessions.auto_authorize_enabled = enabled
            raise RuntimeError("worker synchronization failed")

        self.sessions.set_auto_authorize = fail
        queue = self.hub.subscribe()
        with self.assertRaisesRegex(RuntimeError, "worker synchronization failed"):
            self.client.post("/api/auto-authorize", json={
                "connection_id": self.connection.connection_id, "enabled": False,
            })
        self.assertEqual(self.client.get("/api/auto-authorize").json(), {"enabled": False})
        self.assertEqual(queue.get_nowait().name, "auto_authorize_changed")
        self.hub.unsubscribe(queue)

    def test_model_connection_reports_known_failure_without_echoing_credentials(self):
        self.model_llm.chat.side_effect = LLMAuthenticationError("private-test-key")
        response = self.client.post("/api/model-settings/test", json={
            "connection_id": self.connection.connection_id,
            "model": self.models.config().default_model,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertIn("模型认证失败", response.json()["message"])
        self.assertNotIn("private-test-key", response.text)

    def test_model_connection_rejects_unsaved_model_before_calling_llm(self):
        response = self.client.post("/api/model-settings/test", json={
            "connection_id": self.connection.connection_id, "model": "ollama/unsaved",
        })
        self.assertEqual(response.status_code, 400)
        self.model_llm.chat.assert_not_awaited()

    def test_model_connection_does_not_downgrade_internal_failure(self):
        self.model_llm.chat.side_effect = RuntimeError("internal failure")
        with self.assertRaisesRegex(RuntimeError, "internal failure"):
            self.client.post("/api/model-settings/test", json={
                "connection_id": self.connection.connection_id,
                "model": self.models.config().default_model,
            })

    def test_internal_failure_returns_a_readable_500_without_exposing_traceback(self):
        self.model_llm.chat.side_effect = RuntimeError("private internal failure")
        with TestClient(self.client.app, raise_server_exceptions=False) as client:
            response = client.post("/api/model-settings/test", json={
                "connection_id": self.connection.connection_id,
                "model": self.models.config().default_model,
            })
        self.assertEqual(response.status_code, 500)
        self.assertIn("服务处理请求", response.json()["detail"])
        self.assertNotIn("private internal failure", response.text)

    def test_cancelling_the_event_stream_finishes_and_releases_the_connection(self):
        async def scenario():
            app = self.client.app
            app.state.channel = self.channel
            app.state.hub = self.hub
            endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/events")
            request = SimpleNamespace(app=app, is_disconnected=AsyncMock(return_value=False))
            stream = endpoint(request)
            connected = await anext(stream)
            connection_id = connected.data["connection_id"]
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            with self.assertRaises(web_app.WebConnectionInactive):
                self.channel._require_connection(connection_id)
            self.assertEqual(self.hub._queues, set())
        asyncio.run(scenario())

    def test_model_connection_does_not_downgrade_corrupt_persisted_config(self):
        self.models.path.write_text("{", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.client.post("/api/model-settings/test", json={
                "connection_id": self.connection.connection_id, "model": "ollama/qwen3:8b",
            })
        self.model_llm.chat.assert_not_awaited()

    def test_events_assigns_connection_identity_and_releases_on_disconnect(self):
        body = bytearray()
        headers = {}
        released = []

        async def release(owner):
            # A real release awaits IPC; disconnect cancellation must not skip it.
            import anyio

            await anyio.sleep(0)
            released.append(owner)

        self.sessions.release = release

        async def exercise():
            first = asyncio.Event()

            async def receive():
                await first.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    headers.update(
                        (key.decode(), value.decode())
                        for key, value in message.get("headers", [])
                    )
                elif message["type"] == "http.response.body":
                    chunk = message.get("body") or b""
                    body.extend(chunk)
                    if chunk:
                        first.set()

            async with asyncio.timeout(2):
                await self.client.app(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0", "spec_version": "2.3"},
                        "http_version": "1.1",
                        "method": "GET",
                        "scheme": "http",
                        "path": "/api/events",
                        "raw_path": b"/api/events",
                        "root_path": "",
                        "query_string": b"",
                        "headers": [],
                        "client": ("testclient", 50000),
                        "server": ("testserver", 80),
                    },
                    receive,
                    send,
                )

        self.client.portal.call(exercise)

        self.assertIn("text/event-stream", headers["content-type"])
        event = None
        payload = None
        for line in body.decode().splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1])
        self.assertEqual(event, "connected")
        connection_id = payload["connection_id"]
        self.assertNotIn(connection_id, self.channel._connections)
        self.assertEqual(released, [f"web:{connection_id}"])

    def test_events_sends_keepalive_comments_while_idle(self):
        body = bytearray()
        saw_comment = asyncio.Event()

        async def exercise():
            async def receive():
                await saw_comment.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body":
                    chunk = message.get("body") or b""
                    body.extend(chunk)
                    if b"keep-alive" in chunk:
                        saw_comment.set()

            with patch.object(web_app, "SSE_KEEPALIVE_SECONDS", 0.05):
                async with asyncio.timeout(2):
                    await self.client.app(
                        {
                            "type": "http",
                            "asgi": {"version": "3.0", "spec_version": "2.3"},
                            "http_version": "1.1",
                            "method": "GET",
                            "scheme": "http",
                            "path": "/api/events",
                            "raw_path": b"/api/events",
                            "root_path": "",
                            "query_string": b"",
                            "headers": [],
                            "client": ("testclient", 50000),
                            "server": ("testserver", 80),
                        },
                        receive,
                        send,
                    )

        self.client.portal.call(exercise)
        self.assertIn(b"keep-alive", body)
        self.assertTrue(
            any(call[0] == "release" for call in self.sessions.calls),
        )

    def test_model_settings_never_exposes_credentials(self):
        connections = json.loads(self.models.connections_path.read_text(encoding="utf-8"))
        connections["deepseek"]["api_key"] = "secret-for-test"
        write_json(self.models.connections_path, connections)
        response = self.client.get("/api/model-settings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"], MODEL_CONFIG)
        self.assertNotIn("secret-for-test", response.text)
        status = next(item for item in response.json()["providers"] if item["provider"] == "deepseek")
        self.assertTrue(status["configured"])

    def test_session_model_selection_and_invalid_model_boundary(self):
        response = self.client.get("/api/sessions/session-old/model")
        self.assertEqual(response.json()["selected"]["model"], MODEL_CONFIG["model"]["default"])
        response = self.client.put("/api/sessions/session-old/model", json={
            "connection_id": self.connection.connection_id, "model": "unknown/no-model",
        })
        self.assertEqual(response.status_code, 400)

    def test_model_settings_rejects_deleting_referenced_model(self):
        response = self.client.put("/api/model-settings", json={
            "connection_id": self.connection.connection_id,
            "config": {"model": {"default": None, "candidates": []}},
        })
        self.assertEqual(response.status_code, 409)

    def test_selecting_unknown_session_is_a_client_error(self):
        response = self.client.post(
            "/api/sessions/session-missing/select",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(response.status_code, 404)

    def test_workspace_rename_validates_and_trims_the_display_name(self):
        record = self.workspaces.create(name="demo", task_root=Path(self._directory.name))
        url = f"/api/workspaces/{record.workspace_id}"
        for body in ({"name": " "}, {"name": 12}, {"name": "x", "task_root": "other"}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(url, json=body).status_code, 422)
        self.assertEqual(self.workspaces.get(record.workspace_id), record)

        response = self.client.patch(url, json={"name": " 新名称 "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {**record.to_dict(), "name": "新名称"})
        self.assertEqual(
            self.client.patch("/api/workspaces/workspace-missing", json={"name": "x"}).status_code,
            404,
        )

    def test_deleting_a_workspace_removes_only_the_registry_record(self):
        task_root = Path(self._directory.name) / "task"
        task_root.mkdir()
        record = self.workspaces.create(name="demo", task_root=task_root)

        response = self.client.delete(f"/api/workspaces/{record.workspace_id}")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.workspaces.workspaces, ())
        self.assertTrue(task_root.is_dir())

    def test_deleting_an_unknown_workspace_is_a_client_error(self):
        response = self.client.delete("/api/workspaces/workspace-missing")

        self.assertEqual(response.status_code, 404)
        self.assertIn("workspace-missing", response.json()["detail"])

    def test_accept_input_returns_user_message_and_does_not_wait_for_model(self):
        response = self.client.post(
            "/api/sessions/session-old/inputs",
            json={
                "connection_id": self.connection.connection_id,
                "delivery_id": "delivery-1",
                "text": "  你好  ",
                "interrupt": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["text"], "你好")
        self.assertEqual(response.json()["items"][0]["kind"], "user")
        self.assertEqual(
            self.sessions.calls,
            [
                (
                    "accept_input",
                    "session-old",
                    "你好",
                    {"delivery_id": "delivery-1", "source": "web", "interrupt": True},
                )
            ],
        )

    def test_upload_and_send_image_forwards_session_refs(self):
        uploaded = self.client.post(
            "/api/sessions/session-old/attachments",
            data={"connection_id": self.connection.connection_id},
            files={"file": ("shot.png", _png_bytes(), "image/png")},
        )

        self.assertEqual(uploaded.status_code, 201)
        attachment_id = uploaded.json()["attachment_id"]
        self.assertEqual(uploaded.json()["mime"], "image/png")
        downloaded = self.client.get(
            f"/api/sessions/session-old/attachments/{attachment_id}"
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.headers["content-type"], "image/png")
        self.assertEqual(downloaded.content, _png_bytes())

        sent = self.client.post(
            "/api/sessions/session-old/inputs",
            json={
                "connection_id": self.connection.connection_id,
                "delivery_id": "delivery-image",
                "text": "[Image #1]",
                "artifact_refs": [attachment_id],
            },
        )

        self.assertEqual(sent.status_code, 200)
        self.assertEqual(
            self.sessions.calls[-1],
            (
                "accept_input",
                "session-old",
                "[Image #1]",
                {
                    "delivery_id": "delivery-image",
                    "source": "web",
                    "interrupt": False,
                    "artifact_refs": (attachment_id,),
                },
            ),
        )

    def test_group_endpoints_check_human_input_and_map_known_failures(self):
        record = GroupRecord("group-1", GroupCreated(group="开发组", members=["Ada", "Lin"]))

        async def create(*, name, members):
            if name == "taken":
                raise GroupNameTaken(f"项目内已有群：{name}")
            if "Nobody" in members:
                raise AgentNotFound("Nobody")
            return record

        async def get(group_id):
            if group_id != "group-1":
                raise GroupNotFound(group_id)
            return record

        groups = self.sessions.agent_groups = SimpleNamespace(
            list=AsyncMock(return_value=(record,)), create=AsyncMock(side_effect=create),
            get=AsyncMock(side_effect=get), post=AsyncMock(), stop=AsyncMock(),
            view=AsyncMock(return_value={"group_id": "group-1", "messages": []}))
        connection = self.connection.connection_id
        self.assertEqual(self.client.get("/api/groups").json(), [{
            "group_id": "group-1", "name": "开发组", "members": ["Ada", "Lin"]}])
        create_body = {"connection_id": connection, "name": "开发组"}
        self.assertEqual(self.client.post("/api/groups", json={**create_body, "members": ["Ada", "Lin"]}).status_code, 201)
        for members in (["Ada"], ["Ada", "Ada"], [f"m{index}" for index in range(7)]):
            self.assertEqual(self.client.post("/api/groups", json={**create_body, "members": members}).status_code, 422)
        self.assertEqual(self.client.post("/api/groups", json={
            **create_body, "name": "taken", "members": ["Ada", "Lin"]}).status_code, 409)
        self.assertEqual(self.client.post("/api/groups", json={
            **create_body, "members": ["Ada", "Nobody"]}).status_code, 404)

        post = lambda group_id="group-1", **body: self.client.post(
            f"/api/groups/{group_id}/messages", json={"connection_id": connection, "text": "大家好",
                                                       "delivery_id": "human-message", **body})
        self.assertEqual(post(mention=["Lin"]).status_code, 200)
        groups.post.assert_awaited_once_with("group-1", "大家好", ["Lin"], delivery_id="human-message")
        self.assertEqual(post(mention=["Elsewhere"]).status_code, 422)
        self.assertEqual(post(text="  ").status_code, 422)
        self.assertEqual(post("missing").status_code, 404)
        self.assertEqual(post(connection_id="gone").status_code, 409)
        self.assertEqual(groups.post.await_count, 1)

    def test_routine_fires_are_newest_first_and_point_at_the_delivered_fact(self):
        from redpanda.assistant.conversations import AssistantQueries
        from redpanda.assistant.host.session_store import SessionStore
        from redpanda.automation.once import SOURCE as AUTOMATION_SOURCE
        from redpanda.automation.routines import ROUTINE_DUE
        from redpanda.runtime import DomainFactCommitted, SqliteJournal
        from redpanda.runtime.events import DeliveryIdentity, EventDraft

        clock = RoutineClock(RoutineStore(Path(self._directory.name) / "routines.sqlite"))
        host = SimpleNamespace(routines=clock)
        self.sessions.routine_fires = partial(HostSupervisor.routine_fires, host)
        saved = clock.store.save(
            "cmd-routine", "session-old", routine_id=None, name="检查 PR",
            intent="看看有没有新评审。", schedule="@every 10m",
        )
        routine_id = saved["data"]["routine_id"]
        with patch("redpanda.automation.routines.datetime") as frozen:
            frozen.fromisoformat = datetime.fromisoformat
            frozen.now.return_value = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
            first = clock.store.fire(clock.store.list("session-old")[0])
            clock.store.delivered(first.fire_id)
            frozen.now.return_value = datetime(2026, 10, 20, 10, 10, tzinfo=timezone.utc)
            second = clock.store.fire(clock.store.list("session-old")[0])

        async def record():
            store = SessionStore(Path(self._directory.name) / "sessions")
            await store.create("session-old")
            await SqliteJournal(store.require("session-old")).accept_delivery(EventDraft(
                event_id="routine-due-1",
                session_id="session-old",
                payload=DomainFactCommitted(ROUTINE_DUE, {
                    "routine_id": routine_id,
                    "name": "检查 PR",
                    "intent": "看看有没有新评审。",
                    "planned_at": first.planned_at.isoformat(),
                    "fired_at": first.fired_at.isoformat(),
                }, True),
                occurred_at=datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
                delivery=DeliveryIdentity(AUTOMATION_SOURCE, first.fire_id),
            ))
            return store

        store = asyncio.run(record())
        channel = WebChannel(self.sessions, AssistantQueries(store, self.sessions),
                             AttachmentGateway(Path(self._directory.name)))
        with TestClient(create_web_app(
            channel, self.hub, workspaces=self.workspaces, models=self.models, model_llm=self.model_llm,
        )) as client:
            body = client.get(f"/api/sessions/session-old/routines/{routine_id}/fires").json()
            missing = client.get("/api/sessions/session-old/routines/missing/fires")
            other = client.get(f"/api/sessions/elsewhere/routines/{routine_id}/fires")
        self.assertEqual([item["fire_id"] for item in body], [second.fire_id, first.fire_id])
        self.assertGreater(body[0]["fired_at"], body[1]["fired_at"])
        self.assertIsNone(body[0]["delivered_at"])
        self.assertIsNone(body[0]["output_id"])
        self.assertEqual(body[1]["output_id"], "routine-due-1")
        self.assertIsNotNone(body[1]["delivered_at"])
        self.assertEqual(body[1]["planned_at"], first.planned_at.isoformat())
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(other.status_code, 404)

    def test_a_person_pauses_resumes_and_cancels_an_agents_routine_over_http(self):
        host = SimpleNamespace(routines=RoutineClock(RoutineStore(Path(self._directory.name) / "routines.sqlite")),
                               _emit_schedule_changed=AsyncMock())
        self.sessions.list_routines = partial(HostSupervisor.list_routines, host)
        self.sessions.set_routine_state_by_user = partial(HostSupervisor.set_routine_state_by_user, host)
        routine_id = host.routines.store.save("cmd-routine", "session-old", routine_id=None, name="检查 PR",
                                              intent="看看有没有新评审。", schedule="@every 10m")["data"]["routine_id"]
        url = f"/api/sessions/session-old/routines/{routine_id}/state"
        put = lambda state, connection_id=self.connection.connection_id: self.client.put(
            url, json={"connection_id": connection_id, "state": state})

        listed, = self.client.get("/api/sessions/session-old/routines").json()
        self.assertEqual((listed["name"], listed["schedule"], listed["state"]), ("检查 PR", "@every 10m", "active"))
        self.assertIsNotNone(listed["next_run"])
        paused, = put("paused_by_user").json()
        self.assertEqual((paused["state"], paused["next_run"]), ("paused_by_user", None))
        host._emit_schedule_changed.assert_awaited_with("session-old")
        self.assertEqual(put("active").json()[0]["state"], "active")
        self.assertEqual(put("paused").status_code, 422)
        self.assertEqual(put("deleted", connection_id="gone").status_code, 409)
        self.assertEqual(put("deleted").json(), [])
        self.assertEqual(put("active").status_code, 404)
        self.assertEqual(self.client.get("/api/sessions/session-old/routines").json(), [])

    def test_agent_roster_accepts_a_name_and_project_only(self):
        self.assertEqual(self.client.get("/api/agents").json(), [])
        created = self.client.post("/api/agents", json={
            "connection_id": self.connection.connection_id,
            "workspace_id": "workspace-old",
            "name": "小熊猫",
        })
        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertEqual(body["name"], "小熊猫")
        self.assertEqual(body["projects"], [{"name": "old", "workspace_id": "workspace-old", "directory": "C:/bots/old"}])
        self.assertEqual(body["status"], "waiting")
        self.assertEqual(body["direction"], "前端")
        self.assertNotIn("worktree", body)
        self.assertNotIn("branch", body)
        self.assertEqual(self.sessions.calls[0], ("create_agent", "小熊猫", "workspace-old"))
        self.assertEqual(self.sessions.calls[1][0], "select")

        self.assertEqual(self.client.post("/api/agents", json={
            "connection_id": self.connection.connection_id,
            "workspace_id": "workspace-old",
            "name": "小熊猫",
            "worktree": r"C:\nope",
        }).status_code, 422)
        padded = self.client.post("/api/agents", json={
            "connection_id": self.connection.connection_id,
            "workspace_id": "workspace-old",
            "name": " 小熊猫",
        })
        self.assertEqual(padded.status_code, 422)
        self.assertIn("空白", json.dumps(padded.json(), ensure_ascii=False))
        taken = self.client.post("/api/agents", json={
            "connection_id": self.connection.connection_id,
            "workspace_id": "workspace-old",
            "name": "taken",
        })
        self.assertEqual(taken.status_code, 409)
        self.assertIn("已有 Agent", taken.json()["detail"])
        missing_git = self.client.post("/api/agents", json={
            "connection_id": self.connection.connection_id,
            "workspace_id": "workspace-old",
            "name": "nogit",
        })
        self.assertEqual(missing_git.status_code, 409)
        self.assertIn("Git", missing_git.json()["detail"])

    def test_roster_status_uses_existing_worker_and_wait_facts(self):
        def status(**overrides):
            facts = dict(activity="idle", pending_authorization=False,
                         control_approval=False, waiting_for=(), last_error=None)
            facts.update(overrides)
            return project_agent_status(**facts)

        self.assertEqual(status(activity="running"), "working")
        self.assertEqual(status(activity="running", pending_authorization=True), "working")
        self.assertEqual(status(pending_authorization=True), "authorization")
        self.assertEqual(status(control_approval=True), "authorization")
        self.assertEqual(status(waiting_for=("external_fact",)), "waiting")
        self.assertEqual(status(last_error="失败"), "waiting")
        self.assertEqual(status(), "idle")

    def test_rejected_upload_is_a_client_error(self):
        response = self.client.post(
            "/api/sessions/session-old/attachments",
            data={"connection_id": self.connection.connection_id},
            files={"file": ("shot.png", b"not an image", "image/png")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.json())

    def test_directory_selection_only_returns_a_choice_without_registering_workspace(self):
        selected = Path(self._directory.name)
        for choice in (selected, None):
            with self.subTest(choice=choice), patch.object(
                web_app, "select_directory", return_value=choice
            ):
                response = self.client.post("/api/workspaces/select-directory")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(),
                {"directory": None if choice is None else {
                    "path": str(selected), "name": selected.name,
                }},
            )
            self.assertEqual(self.workspaces.workspaces, ())

    def test_file_selection_attaches_or_cancels_without_returning_a_path(self):
        selected = Path(self._directory.name) / "notes.txt"
        selected.write_text("x")
        for choice in (selected, None):
            with self.subTest(choice=choice), patch(
                "redpanda.channels.web.channel.select_file", return_value=choice
            ):
                response = self.client.post("/api/sessions/session-old/attachments/select",
                    json={"connection_id": self.connection.connection_id})
            self.assertEqual(response.status_code, 200)
            if choice is None:
                self.assertEqual(response.json(), {"file": None})
            else:
                ref = response.json()["file"]
                self.assertEqual((ref["kind"], ref["name"], ref["size"]), ("file", "notes.txt", 1))
                self.assertNotIn("path", ref)
        self.assertNotIn("/api/files/select", {route.path for route in self.client.app.routes})

    def test_known_input_connection_and_authorization_errors_keep_their_meaning(self):
        body = {"connection_id": self.connection.connection_id, "delivery_id": "input-1", "text": " "}
        self.assertEqual(self.client.post("/api/sessions/session-old/inputs", json=body).status_code, 422)
        body.update(connection_id="expired", text="hello")
        self.assertEqual(self.client.post("/api/sessions/session-old/inputs", json=body).status_code, 409)
        with patch("redpanda.channels.web.channel.select_file") as picker:
            self.assertEqual(self.client.post("/api/sessions/session-old/attachments/select",
                json={"connection_id": "expired"}).status_code, 409)
            picker.assert_not_called()
        for error, expected in ((AuthorizationTargetNotFound("目标不存在"), 404), (AuthorizationConflict("当前不可授权"), 409)):
            with self.subTest(error=type(error)), patch.object(self.sessions, "resolve_authorization", side_effect=error):
                response = self.client.post("/api/sessions/session-old/commands/cmd-1/authorize",
                    json={"connection_id": self.connection.connection_id, "approved": True})
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.json()["detail"], str(error))
        with patch.object(self.sessions, "resolve_authorization", side_effect=ValueError("broken contract")), self.assertRaisesRegex(ValueError, "broken contract"):
            self.client.post("/api/sessions/session-old/commands/cmd-1/authorize",
                json={"connection_id": self.connection.connection_id, "approved": True})

    def test_conflicting_input_is_409_but_unknown_input_failure_propagates(self):
        body = {"connection_id": self.connection.connection_id, "delivery_id": "input-1", "text": "hello"}
        message = "该消息编号已用于不同内容，请重新发送消息。"
        with patch.object(self.sessions, "accept_input", side_effect=InputConflict(message)):
            response = self.client.post("/api/sessions/session-old/inputs", json=body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": message})
        with patch.object(self.sessions, "accept_input", side_effect=ValueError("broken contract")), self.assertRaisesRegex(ValueError, "broken contract"):
            self.client.post("/api/sessions/session-old/inputs", json=body)

    def test_directory_picker_only_converts_known_desktop_unavailability(self):
        with patch.object(
            web_app, "select_directory",
            side_effect=DirectoryPickerUnavailable("当前 Python 未安装 Tcl/Tk"),
        ):
            response = self.client.post("/api/workspaces/select-directory")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "当前 Python 未安装 Tcl/Tk"})

        with patch.object(
            web_app, "select_directory", side_effect=ValueError("broken protocol")
        ), self.assertRaisesRegex(ValueError, "broken protocol"):
            self.client.post("/api/workspaces/select-directory")

    def test_create_workspace_rejects_missing_directory(self):
        response = self.client.post(
            "/api/workspaces",
            json={
                "name": "missing",
                "task_root": str(Path(self._directory.name) / "nope"),
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_create_workspace_rejects_duplicate_path(self):
        body = {
            "name": "demo",
            "task_root": self._directory.name,
            "full_access": False,
        }
        first = self.client.post("/api/workspaces", json=body)
        second = self.client.post("/api/workspaces", json=body)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)


    def test_inbox_returns_pending_authorization_and_control(self):
        response = self.client.get("/api/inbox")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [
            {
                "kind": "authorization",
                "session_id": "session-old",
                "workspace_id": "workspace-old",
                "label": "验证熊猫",
                "command_id": "cmd-1",
                "name": "apply_patch",
                "arguments": {"path": "note.txt"},
            },
            {
                "kind": "control",
                "session_id": "session-old",
                "workspace_id": "workspace-old",
                "label": "验证熊猫",
                "request_id": "req-1",
                "summary": "删除工作区",
                "risk": "high",
            },
        ])


class ReportWorkerFailuresTest(unittest.IsolatedAsyncioTestCase):
    async def test_compact_reader_failure_is_not_session_failed(self):
        host = SimpleNamespace(
            compact=SimpleNamespace(
                store=SimpleNamespace(
                    reader_job=lambda session_id: (
                        {"source": "chat"} if session_id == "compact-1" else None
                    )
                )
            )
        )
        pending = [
            WorkerFailed("compact-1", ProcessFailure("Boom", "reader died", "")),
            WorkerFailed("session-1", ProcessFailure("Dead", "worker died", "")),
        ]

        async def wait_failure():
            if pending:
                return pending.pop(0)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        host.wait_failure = wait_failure
        events = WebEventHub()
        queue = events.subscribe()
        task = asyncio.create_task(report_worker_failures(host, events))
        event = await asyncio.wait_for(queue.get(), timeout=1)
        self.assertEqual(event.name, "session_failed")
        self.assertEqual(event.data["session_id"], "session-1")
        self.assertTrue(queue.empty())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        events.unsubscribe(queue)


def _png_bytes():
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    return buffer.getvalue()
