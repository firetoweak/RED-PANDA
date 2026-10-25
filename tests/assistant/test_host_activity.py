from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from unittest.mock import AsyncMock

from konden.assistant.host.session_store import SessionStore
from konden.assistant.host.supervisor import HostSupervisor, Worker
from konden.assistant.subagent.subagent import (
    DELEGATE,
    DelegateIntent,
    child_session_id,
    persist_return,
    return_data,
    task_fact_arguments,
)
from konden.paths import KondenHome
from konden.runtime import (
    CommandPhase,
    replay,
    AgentRuntime,
    InvokeTool,
    ModelDecision,
    SqliteJournal,
    ToolBinding,
    UserMessageReceived,
)
from konden.runtime.events import DeliveryIdentity, EventDraft
from konden.filesystem.registry import WorkspaceRegistry
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


class HostStopChildrenTest(unittest.IsolatedAsyncioTestCase):
    async def test_failure_stopping_one_child_does_not_leave_others_unattempted(self):
        host = object.__new__(HostSupervisor)
        host.turn_locks = {}
        host.workers = {}
        host.compact = SimpleNamespace(application=AsyncMock())
        host.store = SimpleNamespace(require=lambda _session: "journal")
        host._running_children = AsyncMock(return_value=("child-1", "child-2"))
        host.wait_quiescent = AsyncMock()
        attempted = []
        failure = OSError("child teardown failed")

        async def reclaim(child, _arguments):
            attempted.append(child)
            if child == "child-1":
                raise failure

        host._reclaim_child = reclaim
        with self.assertRaises(OSError) as caught:
            await host.cancel_turn("parent")
        self.assertIs(caught.exception, failure)
        self.assertEqual(set(attempted), {"child-1", "child-2"})
        host.wait_quiescent.assert_not_awaited()


class SelectIdleSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_select_waiting_session_binds_owner_without_starting_worker(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = KondenHome(root / "home")
            home.initialize()
            task_root = root / "workspace"
            task_root.mkdir()
            workspaces = WorkspaceRegistry.load(home.workspaces_path)
            workspace = workspaces.create(

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


class ResumeUnfinishedOnStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_recovers_unknown_commands_without_retrying_them(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = KondenHome(root / "home")
            home.initialize()
            task_root = root / "workspace"
            task_root.mkdir()
            workspaces = WorkspaceRegistry.load(home.workspaces_path)
            workspace = workspaces.create(task_root=task_root)
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

                # 上一个进程死在命令执行中：Attempt 已开始，没有结果。
                await host.create("session-crashed", workspace.workspace_id)

                async def unfinished(_context, _arguments):
                    await asyncio.Event().wait()

                crashed = AgentRuntime(
                    SqliteJournal(store.require("session-crashed")),
                    ScriptedDecisionMaker((
                        lambda _: ModelDecision(command_requests=(InvokeTool("execute_command"),)),
                    )),
                    {"execute_command": ToolBinding(unfinished)},
                )
                await crashed.receive_user_message(
                    "session-crashed", "跑命令", delivery_id="d2"
                )
                await crashed.advance("session-crashed")
                await crashed.dispatcher.close()

                host.resume = AsyncMock()

                await host.resume_unfinished_sessions()

                resumed = {call.args[0] for call in host.resume.await_args_list}
                self.assertEqual(resumed, {"session-open", "session-crashed"})
                state = replay("session-crashed", await SqliteJournal(
                    store.require("session-crashed")).snapshot("session-crashed")).state
                self.assertEqual([command.phase for command in state.commands], [CommandPhase.UNKNOWN])
                # 下一步交给模型核对，Host 不重试原命令。
                reviewed = AgentRuntime(
                    SqliteJournal(store.require("session-crashed")),
                    ScriptedDecisionMaker((lambda _: ModelDecision(content="已核对中断现场"),)),
                    {"execute_command": ToolBinding(unfinished)},
                )
                await reviewed.advance("session-crashed")
                await reviewed.dispatcher.close()
                await reviewed.receive_user_message("session-crashed", "下一项", delivery_id="next")
                host.resume.reset_mock()
                await host.resume_unfinished_sessions()
                self.assertIn("session-crashed", {call.args[0] for call in host.resume.await_args_list})
            finally:
                await host.close()


class QuiescentDespiteUnreportedChildTest(unittest.IsolatedAsyncioTestCase):
    async def test_returned_child_does_not_block_quiescence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = KondenHome(root / "home")
            home.initialize()
            task_root = root / "workspace"
            task_root.mkdir()
            workspaces = WorkspaceRegistry.load(home.workspaces_path)
            workspace = workspaces.create(task_root=task_root)
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
                    return_data(child, failure="限流失败"),
                )
                worker = Worker(
                    process=SimpleNamespace(is_alive=lambda: False),
                    peer=object(),
                )
                worker.idle_revision = 1
                worker.idle_has_open_subagents = True
                host.workers["parent"] = worker
                host.view = AsyncMock(return_value="quiet")

                self.assertEqual(await host.wait_quiescent("parent"), "quiet")
            finally:
                await host.close()
