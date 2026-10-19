from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from redpanda.assistant.agent_messages import MESSAGE_USER
from redpanda.assistant.attachments import AttachmentGateway
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome


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

    async def test_message_user_copies_a_worktree_file_and_rejects_paths_outside_it(self):
        agent = await self.host.create_agent(name="Ada", workspace_id=self.workspace.workspace_id)
        agent.worktree.mkdir(parents=True, exist_ok=True)
        (agent.worktree / "evidence.txt").write_bytes(b"report")
        delivered = await self.host._route(MESSAGE_USER, agent.session_id, {
            "output_id": "out-1", "text": "证据在这", "path": "evidence.txt",
        })
        attachment = delivered["attachment"]
        stored = AttachmentGateway(self.store.root).for_session(agent.session_id).files.source(
            attachment["attachment_id"])
        self.assertEqual(stored.read_bytes(), b"report")
        self.assertEqual(attachment["name"], "evidence.txt")
        for path in ("../evidence.txt", "missing.txt", str(agent.worktree / "evidence.txt")):
            refused = await self.host._route(MESSAGE_USER, agent.session_id, {
                "output_id": "out-2", "text": "不行", "path": path,
            })
            self.assertFalse(refused["ok"], path)
