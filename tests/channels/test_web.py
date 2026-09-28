from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from helperme.assistant.attachments import AttachmentGateway
from helperme.assistant.host.ipc import ProcessFailure, WorkerFailed
from helperme.assistant.conversations import (
    ConversationView,
    SessionSummary,
    UserItem,
)
from helperme.assistant.runner import SessionNotFoundError
from helperme.assistant.sessions import SessionView
from helperme.assistant.workspace_versions import StepNotRewindable
from helperme.channels.web import app as web_app
from helperme.channels.web.app import create_web_app, report_worker_failures
from helperme.channels.web.channel import WebChannel
from helperme.channels.web.directory_picker import DirectoryPickerUnavailable
from helperme.channels.web.hub import WebEventHub
from helperme.sandbox.registry import WorkspaceRegistry


class _Sessions:
    def __init__(self, queries):
        self.queries = queries
        self.calls = []
        self.control_message = None

    async def create(self, session_id, workspace_id):
        self.calls.append(("create", session_id, workspace_id))

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

    async def restart_from_step(
        self, owner, session_id, step_id, child_session_id, delivery_id
    ):
        self.calls.append(
            ("restart_from_step", owner, session_id, step_id, child_session_id, delivery_id)
        )
        if step_id == "step-unrecorded":
            raise StepNotRewindable(step_id)
        self.queries.record(child_session_id, "重开前说过的话")
        return SessionView("runnable", (), (), False, paused=True)

    async def fork_and_accept_input(
        self, owner, source_session_id, message_id, content, **kwargs
    ):
        self.calls.append(
            (
                "fork_and_accept_input",
                owner,
                source_session_id,
                message_id,
                content,
                kwargs,
            )
        )
        self.queries.record(kwargs["child_session_id"], content)
        return SessionView("runnable", (), (), True)

    async def branch_after_turn(
        self, owner, source_session_id, message_id, child_session_id
    ):
        self.calls.append(
            ("branch_after_turn", owner, source_session_id, message_id, child_session_id)
        )
        self.queries.record(child_session_id, "这一轮已经说完")
        return SessionView("waiting", ("external_fact",), (), False)

    async def release(self, owner):
        self.calls.append(("release", owner))

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

    async def set_auto_authorize(self, session_id, enabled):
        self.calls.append(("set_auto_authorize", session_id, enabled))
        return SessionView("waiting", ("external_fact",), (), False, auto_authorize=enabled)

    async def set_paused(self, session_id, paused):
        self.calls.append(("set_paused", session_id, paused))
        return SessionView("waiting", ("external_fact",), (), False, paused=paused)

    def session_titles(self):
        return {"session-old": "改过的标题"}

    async def archive(self, session_id):
        self.calls.append(("archive", session_id))

    def set_title(self, session_id, title):
        self.calls.append(("set_title", session_id, title))


class _Queries:
    def __init__(self):
        self.accepted: list[tuple[str, str]] = []

    def record(self, session_id, content):
        self.accepted.append((session_id, content))

    async def list_sessions(self):
        return (
            SessionSummary("session-old", "workspace-old", "旧会话", None, "idle"),
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
        self.channel = WebChannel(
            self.sessions,
            self.queries,
            AttachmentGateway(Path(self._directory.name)),
        )
        self.hub = WebEventHub()
        self.connection = self.channel.connect()
        self.workspaces = WorkspaceRegistry.load(
            Path(self._directory.name) / "workspaces.json"
        )
        self.client = TestClient(
            create_web_app(self.channel, self.hub, workspaces=self.workspaces)
        )
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._directory.cleanup()

    def test_lists_sessions_and_creates_selected_conversation(self):
        listed = self.client.get("/api/sessions")
        created = self.client.post(
            "/api/sessions",
            json={
                "connection_id": self.connection.connection_id,
                "workspace_id": "workspace-old",
            },
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()[0]["title"], "旧会话")
        self.assertEqual(created.status_code, 201)
        session_id = created.json()["session_id"]
        self.assertEqual(
            self.sessions.calls[0],
            ("create", session_id, "workspace-old"),
        )
        self.assertEqual(
            self.sessions.calls[1],
            ("select", self.connection.owner, session_id),
        )

    def test_events_assigns_connection_identity_and_releases_on_disconnect(self):
        body = bytearray()
        headers = {}

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
        self.assertIn(("release", f"web:{connection_id}"), self.sessions.calls)

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

    def test_reads_conversation_without_starting_a_worker(self):
        response = self.client.get("/api/sessions/session-old")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], "session-old")
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(response.json()["compact_count"], 0)
        self.assertIsNone(response.json()["compact_phase"])
        self.assertEqual(self.sessions.calls, [])

    def test_runtime_exposes_active_model_and_compact_threshold_tokens(self):
        response = self.client.get("/api/runtime")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"model": "test", "compact_threshold_tokens": 200000},
        )

    def test_selecting_unknown_session_is_a_client_error(self):
        response = self.client.post(
            "/api/sessions/session-missing/select",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(response.status_code, 404)

    def test_selecting_another_session_only_changes_owner_selection(self):
        response = self.client.post(
            "/api/sessions/session-old/select",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.sessions.calls,
            [("select", self.connection.owner, "session-old")],
        )

    def test_accept_input_returns_user_message_and_does_not_wait_for_model(self):
        response = self.client.post(
            "/api/sessions/session-old/inputs",
            json={
                "connection_id": self.connection.connection_id,
                "delivery_id": "delivery-1",
                "text": "  你好  ",
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
                    {"delivery_id": "delivery-1", "source": "web"},
                )
            ],
        )

    def test_cancel_only_targets_the_requested_session(self):
        response = self.client.post(
            "/api/sessions/session-old/cancel",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sessions.calls, [("cancel_turn", "session-old")])

    def test_retry_wakes_the_requested_session(self):
        response = self.client.post(
            "/api/sessions/session-old/retry",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sessions.calls, [("retry", "session-old")])

    def test_restarting_from_a_step_answers_with_the_new_identity(self):
        """从一步重开产生新身份，源会话留在原处；没有版本的一步直接拒绝。"""
        done = self.client.post(
            "/api/sessions/session-old/restarts",
            json={
                "connection_id": self.connection.connection_id,
                "delivery_id": "web-1",
                "step_id": "step-1",
            },
        )
        self.assertEqual(done.status_code, 201)
        child_session_id = done.json()["session_id"]
        self.assertNotEqual(child_session_id, "session-old")
        self.assertTrue(done.json()["session"]["paused"])

        refused = self.client.post(
            "/api/sessions/session-old/restarts",
            json={
                "connection_id": self.connection.connection_id,
                "delivery_id": "web-2",
                "step_id": "step-unrecorded",
            },
        )
        self.assertEqual(refused.status_code, 409)
        self.assertIn("没有成功的版本记录", refused.json()["detail"])
        self.assertEqual(
            [(call[0], call[3], call[4]) for call in self.sessions.calls],
            [
                ("restart_from_step", "step-1", child_session_id),
                ("restart_from_step", "step-unrecorded", self.sessions.calls[1][4]),
            ],
        )

    def test_branching_after_a_turn_creates_a_listed_session(self):
        response = self.client.post(
            "/api/sessions/session-old/branches",
            json={
                "connection_id": self.connection.connection_id,
                "message_id": "user-1",
            },
        )

        self.assertEqual(response.status_code, 201)
        child_session_id = response.json()["session_id"]
        self.assertNotEqual(child_session_id, "session-old")
        self.assertEqual(
            self.sessions.calls,
            [
                (
                    "branch_after_turn",
                    self.connection.owner,
                    "session-old",
                    "user-1",
                    child_session_id,
                )
            ],
        )

    def test_editing_user_message_creates_and_selects_a_new_branch(self):
        response = self.client.post(
            "/api/sessions/session-old/forks",
            json={
                "connection_id": self.connection.connection_id,
                "message_id": "user-1",
                "delivery_id": "edit-1",
                "text": "  修改后  ",
            },
        )

        self.assertEqual(response.status_code, 201)
        child_session_id = response.json()["session_id"]
        self.assertNotEqual(child_session_id, "session-old")
        self.assertEqual(response.json()["items"][0]["text"], "修改后")
        self.assertEqual(
            self.sessions.calls,
            [
                (
                    "fork_and_accept_input",
                    self.connection.owner,
                    "session-old",
                    "user-1",
                    "修改后",
                    {
                        "child_session_id": child_session_id,
                        "delivery_id": "edit-1",
                        "source": "web",
                        "listed": False,
                        "restore_files": False,
                    },
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
                    "artifact_refs": (attachment_id,),
                },
            ),
        )

    def test_resolve_control_returns_execution_message(self):
        response = self.client.post(
            "/api/sessions/session-old/control",
            json={
                "connection_id": self.connection.connection_id,
                "request_id": "approval-1",
                "approved": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["session"]["control_message"],
            "MCP Server `demo` 安装、测试并启用成功。能力目录已更新，load_toolset 之后工具从下一个 Step 可见。",
        )
        self.assertEqual(
            self.sessions.calls,
            [
                ("resolve_control", "session-old", "approval-1", True),
                ("view", "session-old"),
            ],
        )

    def test_authorize_command_is_per_command(self):
        response = self.client.post(
            "/api/sessions/session-old/commands/cmd-1/authorize",
            json={
                "connection_id": self.connection.connection_id,
                "approved": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.sessions.calls,
            [
                ("resolve_authorization", "session-old", "cmd-1", True),
                ("view", "session-old"),
            ],
        )

    def test_auto_authorize_updates_session_preference(self):
        response = self.client.post(
            "/api/sessions/session-old/auto-authorize",
            json={
                "connection_id": self.connection.connection_id,
                "enabled": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["session"]["auto_authorize"])
        self.assertEqual(
            self.sessions.calls,
            [("set_auto_authorize", "session-old", True)],
        )

    def test_paused_updates_session_hold(self):
        response = self.client.post(
            "/api/sessions/session-old/paused",
            json={
                "connection_id": self.connection.connection_id,
                "paused": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["session"]["paused"])
        self.assertEqual(
            self.sessions.calls,
            [("set_paused", "session-old", True)],
        )

    def test_lists_session_titles(self):
        response = self.client.get("/api/session-titles")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"session-old": "改过的标题"})

    def test_archive_hides_the_session_from_the_list(self):
        response = self.client.post(
            "/api/sessions/session-old/archive",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})
        self.assertEqual(self.sessions.calls, [("archive", "session-old")])

    def test_title_override_is_a_product_label(self):
        response = self.client.post(
            "/api/sessions/session-old/title",
            json={
                "connection_id": self.connection.connection_id,
                "title": "  我起的名  ",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"title": "我起的名"})
        self.assertEqual(
            self.sessions.calls,
            [("set_title", "session-old", "我起的名")],
        )

    def test_rejected_upload_is_a_client_error(self):
        response = self.client.post(
            "/api/sessions/session-old/attachments",
            data={"connection_id": self.connection.connection_id},
            files={"file": ("shot.png", b"not an image", "image/png")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.json())

    def test_lists_and_creates_workspaces(self):
        listed = self.client.get("/api/workspaces")
        created = self.client.post(
            "/api/workspaces",
            json={
                "name": "demo",
                "task_root": self._directory.name,
                "full_access": False,
            },
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json(), [])
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["name"], "demo")
        self.assertEqual(len(self.client.get("/api/workspaces").json()), 1)

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

    def test_file_selection_only_returns_a_choice_without_attaching(self):
        selected = Path(self._directory.name) / "notes.txt"
        selected.write_text("x")
        for choice in (selected, None):
            with self.subTest(choice=choice), patch.object(
                web_app, "select_file", return_value=choice
            ):
                response = self.client.post("/api/files/select")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(),
                {"file": None if choice is None else {
                    "path": str(selected), "name": selected.name,
                }},
            )

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
