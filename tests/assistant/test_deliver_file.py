from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from redpanda.assistant.agent_messages import SEND_FILE, message_bindings
from redpanda.assistant.attachments import AttachmentGateway, AttachmentRejected
from redpanda.assistant.conversations import project_conversation
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome
from redpanda.assistant.sessions import session_view
from redpanda.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal, replay
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.session_scheduler import settle_session


class DeliverFileTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name).resolve()
        self.home = RedPandaHome(root / "home")
        self.store = SessionStore(self.home.runtime_sessions_root)
        project = root / "project"
        project.mkdir()
        registry = WorkspaceRegistry.load(self.home.workspaces_path)
        self.workspace = registry.register_path(project)
        self.host = HostSupervisor(self.store, None, self.home, None, llm=None, workspaces=registry)
        self.git = patch.object(
            self.host.agent_worktrees, "create",
            side_effect=lambda workspace, session_id: self.host.agent_worktrees.agent(
                workspace.workspace_id, session_id),
        )
        self.git.start()
        self.requests = patch.object(self.host, "request", AsyncMock())
        self.requests.start()

    async def asyncTearDown(self):
        self.requests.stop()
        self.git.stop()
        await self.host.close()
        self.directory.cleanup()

    async def test_send_file_copies_a_worktree_file_and_rejects_paths_outside_it(self):
        agent = await self.host.create_agent(name="Ada", workspace_id=self.workspace.workspace_id)
        agent.worktree.mkdir(parents=True, exist_ok=True)
        (agent.worktree / "evidence.txt").write_bytes(b"report")
        delivered = await self.host._route(SEND_FILE, agent.session_id, {
            "output_id": "out-1", "text": "证据在这", "path": "evidence.txt",
        })
        attachment = delivered["attachment"]
        stored = AttachmentGateway(self.store.root).for_session(agent.session_id).files.source(
            attachment["attachment_id"])
        self.assertEqual(stored.read_bytes(), b"report")
        self.assertEqual(attachment["name"], "evidence.txt")
        for path in ("../evidence.txt", "missing.txt", str(agent.worktree / "evidence.txt")):
            refused = await self.host._route(SEND_FILE, agent.session_id, {
                "output_id": "out-2", "text": "不行", "path": path,
            })
            self.assertFalse(refused["ok"], path)

    async def test_unreadable_file_is_a_known_error_and_does_not_escape(self):
        from redpanda.assistant.file_attachments import FileAttachmentStore

        agent = await self.host.create_agent(name="Ada", workspace_id=self.workspace.workspace_id)
        agent.worktree.mkdir(parents=True, exist_ok=True)
        (agent.worktree / "noread.txt").write_bytes(b"x")
        for error in (PermissionError(13, "denied"), AttachmentRejected("附件名称必须是有效的单个文件名")):
            with patch.object(FileAttachmentStore, "save_stream", side_effect=error):
                refused = await self.host._route(SEND_FILE, agent.session_id, {
                    "output_id": "out-locked", "text": "带上", "path": "noread.txt",
                })
            self.assertEqual(refused["code"], "FILE_UNREADABLE")

    async def test_file_tool_preserves_download_in_history_and_requires_a_file(self):
        agent = await self.host.create_agent(name="Ada", workspace_id=self.workspace.workspace_id)
        agent.worktree.mkdir(parents=True, exist_ok=True)
        (agent.worktree / "evidence.txt").write_bytes(b"report")
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
