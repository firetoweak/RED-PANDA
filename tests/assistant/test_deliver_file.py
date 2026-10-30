from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from konden.assistant.agent_messages import SEND_FILE, message_bindings
from konden.assistant.attachments import AttachmentGateway, AttachmentRejected
from konden.assistant.conversations import project_conversation
from konden.assistant.host.session_store import SessionStore
from konden.assistant.host.supervisor import HostSupervisor
from konden.paths import KondenHome
from konden.assistant.sessions import session_view
from konden.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal, replay
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.session_scheduler import settle_session


class DeliverFileTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name).resolve()
        self.home = KondenHome(root / "home")
        self.store = SessionStore(self.home.runtime_sessions_root)
        self.host = HostSupervisor(self.store, None, self.home, None, llm=None)
        self.requests = patch.object(self.host, "request", AsyncMock())
        self.requests.start()

    async def asyncTearDown(self):
        self.requests.stop()
        await self.host.close()
        self.directory.cleanup()

    async def test_send_file_copies_a_worktree_file_and_rejects_paths_outside_it(self):
        agent = await self.host.create_agent(name="Ada")
        agent.directory.mkdir(parents=True, exist_ok=True)
        (agent.directory / "evidence.txt").write_bytes(b"report")
        delivered = await self.host._route(SEND_FILE, agent.session_id, {
            "output_id": "out-1", "text": "证据在这", "path": "evidence.txt",
        })
        attachment = delivered["attachment"]
        stored = AttachmentGateway(self.store.root).for_session(agent.session_id).files.source(
            attachment["attachment_id"])
        self.assertEqual(stored.read_bytes(), b"report")
        self.assertEqual(attachment["name"], "evidence.txt")
        for path in ("../evidence.txt", "missing.txt", str(agent.directory / "evidence.txt")):
            refused = await self.host._route(SEND_FILE, agent.session_id, {
                "output_id": "out-2", "text": "不行", "path": path,
            })
            self.assertFalse(refused["ok"], path)

    async def test_unreadable_file_is_a_known_error_and_does_not_escape(self):
        from konden.assistant.file_attachments import FileAttachmentStore

        agent = await self.host.create_agent(name="Ada")
        agent.directory.mkdir(parents=True, exist_ok=True)
        (agent.directory / "noread.txt").write_bytes(b"x")
        for error in (PermissionError(13, "denied"), AttachmentRejected("附件名称必须是有效的单个文件名")):
            with patch.object(FileAttachmentStore, "save_stream", side_effect=error):
                refused = await self.host._route(SEND_FILE, agent.session_id, {
                    "output_id": "out-locked", "text": "带上", "path": "noread.txt",
                })
            self.assertEqual(refused["code"], "FILE_UNREADABLE")

    async def test_picture_without_a_caption_is_delivered_and_survives_replay(self):
        from PIL import Image

        agent = await self.host.create_agent(name="Ada")
        agent.directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), "blue").save(agent.directory / "picture.png")
        journal = SqliteJournal(self.store.require(agent.session_id))
        runtime = AgentRuntime(journal, ScriptedDecisionMaker((
            lambda _: ModelDecision(command_requests=(InvokeTool(SEND_FILE, (("path", "picture.png"),)),)),
            lambda _: ModelDecision(content="蓝色背景突出中央主体。"),
        )), message_bindings(self.host._route, journal))
        await settle_session(runtime, agent.session_id)
        reopened = SqliteJournal(self.store.require(agent.session_id))
        events = await reopened.snapshot(agent.session_id)
        state = replay(agent.session_id, events).state
        view = project_conversation(agent.session_id, events, state.steps, session=session_view(state),
            attachments=AttachmentGateway(self.store.root).for_session(agent.session_id))
        picture, = [item for item in view.items if item.kind == "step" and item.files]
        self.assertIsNone(picture.text)
        self.assertEqual(picture.files[0].name, "picture.png")
        self.assertEqual([item.text for item in view.items if item.kind == "step" and item.text],
                         ["蓝色背景突出中央主体。"])

    async def test_file_tool_preserves_download_in_history_and_requires_a_file(self):
        agent = await self.host.create_agent(name="Ada")
        agent.directory.mkdir(parents=True, exist_ok=True)
        (agent.directory / "evidence.txt").write_bytes(b"report")
        journal = SqliteJournal(self.store.require(agent.session_id))
        runtime = AgentRuntime(journal, ScriptedDecisionMaker((
            lambda _: ModelDecision(command_requests=(InvokeTool(SEND_FILE, (("text", "缺少文件"),)),)),
            lambda _: ModelDecision(command_requests=(InvokeTool(SEND_FILE, (
                ("text", "证据在这"), ("path", "evidence.txt"))),)),
            lambda _: ModelDecision(content="交付完成"),
        )), message_bindings(self.host._route, journal))
        await settle_session(runtime, agent.session_id)
        events = await journal.snapshot(agent.session_id)
        state = replay(agent.session_id, events).state
        view = project_conversation(agent.session_id, events, state.steps, session=session_view(state),
                                    attachments=AttachmentGateway(self.store.root).for_session(agent.session_id))
        files = [item for item in view.items if item.kind == "step" and item.files]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].text, "证据在这")
        self.assertEqual(files[0].files[0].name, "evidence.txt")
        self.assertEqual([item.text for item in view.items if item.kind == "step" and item.text],
                         ["证据在这", "交付完成"])
