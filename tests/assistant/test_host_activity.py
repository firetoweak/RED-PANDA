from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from unittest.mock import AsyncMock

from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor, Worker
from redpanda.assistant.subagent.subagent import (
    DELEGATE,
    DelegateIntent,
    child_session_id,
    persist_return,
    return_data,
    task_fact_arguments,
)
from redpanda.paths import RedPandaHome
from redpanda.runtime import (
    AgentRuntime,
    InvokeTool,
    ModelDecision,
    SqliteJournal,
    ToolBinding,
    UserMessageReceived,
)
from redpanda.runtime.events import DeliveryIdentity, EventDraft
from redpanda.sandbox.registry import WorkspaceRegistry
from tests.assistant.test_runner import ScriptedDecisionMaker


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


class PauseUnfinishedOnStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_runnable_top_level_sessions_pause_and_are_not_resumed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = RedPandaHome(root / "home")
            home.initialize()
            task_root = root / "workspace"
            task_root.mkdir()
            workspaces = WorkspaceRegistry.load(home.workspaces_path)
            workspace = workspaces.create(name="demo", task_root=task_root)
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
                await host.create("session-waiting", workspace.workspace_id)
                await host.create("session-open", workspace.workspace_id)
                await SqliteJournal(store.require("session-open")).accept_delivery(
                    EventDraft(
                        event_id="user-1",
                        session_id="session-open",
                        payload=UserMessageReceived("接着做"),
                        occurred_at=datetime.now(timezone.utc),
                        delivery=DeliveryIdentity("user", "d1"),
                    )
                )
                child = child_session_id("session-open", "command-1")
                await store.create(
                    child,
                    workspace_id=workspace.workspace_id,
                    initial_fact=task_fact_arguments(
                        DelegateIntent("command-1", "session-open", child, "做完这件事")
                    ),
                )

                await host.pause_unfinished_sessions()

                self.assertFalse(host.is_paused("session-waiting"))
                self.assertTrue(host.is_paused("session-open"))
                self.assertFalse(host.is_paused(child))
                view = await host.select("web", "session-open")
                self.assertTrue(view.paused)
                self.assertTrue(view.should_wake)
                self.assertEqual(host.workers, {})
            finally:
                await host.close()


class QuiescentDespiteUnreportedChildTest(unittest.IsolatedAsyncioTestCase):
    async def test_returned_child_does_not_block_quiescence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = RedPandaHome(root / "home")
            home.initialize()
            task_root = root / "workspace"
            task_root.mkdir()
            workspaces = WorkspaceRegistry.load(home.workspaces_path)
            workspace = workspaces.create(name="demo", task_root=task_root)
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
                await store.create("parent", workspace_id=workspace.workspace_id)

                async def delegate(_context, _arguments):
                    return "派出"

                runtime = AgentRuntime(
                    SqliteJournal(store.require("parent")),
                    ScriptedDecisionMaker((
                        lambda _: ModelDecision(command_requests=(
                            InvokeTool(DELEGATE, (("task", "调查问题"),)),
                        )),
                    )),
                    {DELEGATE: ToolBinding(delegate, requires_authorization=True)},
                )
                await runtime.receive_user_message("parent", "调查", delivery_id="user")
                advance = await runtime.advance("parent")
                command_id = advance.step.commands[0].command_id
                child = child_session_id("parent", command_id)
                await store.create(child, workspace_id=workspace.workspace_id)
                await persist_return(
                    SqliteJournal(store.require(child)),
                    child,
                    return_data(child, reported=False, summary=None, failure="限流失败"),
                )
                worker = Worker(
                    process=SimpleNamespace(is_alive=lambda: False),
                    peer=object(),
                )
                worker.idle_revision = 1
                worker.idle_has_active_subagents = True
                host.workers["parent"] = worker
                host.view = AsyncMock(return_value="quiet")

                self.assertFalse(await host._children_still_running("parent"))
                self.assertEqual(await host.wait_quiescent("parent"), "quiet")
            finally:
                await host.close()
