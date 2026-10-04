from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor, Worker
from redpanda.paths import RedPandaHome
from redpanda.sandbox.registry import WorkspaceRegistry


class HostActivityTest(unittest.TestCase):
    def test_activity_follows_scheduler_busy_not_host_requests(self):
        host = object.__new__(HostSupervisor)
        host.workers = {}
        self.assertEqual(host.activity("session-1"), "idle")

        worker = Worker(process=object(), peer=object())
        host.workers["session-1"] = worker
        self.assertEqual(host.activity("session-1"), "idle")

        worker.running = True
        worker.idle_revision = None
        self.assertEqual(host.activity("session-1"), "running")

        worker.running = False
        worker.idle_revision = 3
        self.assertEqual(host.activity("session-1"), "idle")


class SelectIdleSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_select_waiting_session_binds_owner_without_starting_worker(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = RedPandaHome(root / "home")
            home.initialize()
            task_root = root / "workspace"
            task_root.mkdir()
            workspaces = WorkspaceRegistry.load(home.workspaces_path)
            workspace = workspaces.create(
                name="demo",
                task_root=task_root,
            )
            store = SessionStore(home.runtime_sessions_root)
            host = HostSupervisor(
                store,
                lambda: None,
                home,
                lambda *_values: None,
                llm=object(),
                workspaces=workspaces,
            )
            try:
                await host.create("session-1", workspace.workspace_id)
                view = await host.select("web", "session-1")
                self.assertEqual(view.status, "waiting")
                self.assertFalse(view.should_wake)
                self.assertEqual(host.selections["web"], "session-1")
                self.assertEqual(host.workers, {})
                self.assertEqual(host.activity("session-1"), "idle")
            finally:
                await host.close()
