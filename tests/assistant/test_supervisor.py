from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
import tempfile
import time
import unittest

import pytest

from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.host.supervisor import HostSupervisor
from helperme.assistant.subagent.subagent import (
    DelegateIntent,
    project_delegations,
    project_reclaimed,
    task_fact_arguments,
)
from helperme.paths import HelperMeHome
from helperme.runtime import SqliteJournal
from tests.fixtures.session_worker import (
    CancellableProcessLlm,
    ProcessLlm,
    cancellable_config,
    config_for,
    interrupted_read_config,
    failing_startup_config,
    failing_request_config,
)

pytestmark = pytest.mark.process

PARENT = "parent"
MANUAL_CHILD_COMMAND = "manual-child"
CHILD = f"{PARENT}/sub-{MANUAL_CHILD_COMMAND}"


def child_task(task: str) -> dict[str, object]:
    return task_fact_arguments(
        DelegateIntent(MANUAL_CHILD_COMMAND, PARENT, CHILD, task)
    )


async def until(predicate, timeout=30):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def test_web_can_reject_journal_authorization_after_host_restart(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from helperme.assistant.conversations import AssistantQueries
        from helperme.channels.web.channel import WebChannel
        from helperme.runtime import AgentRuntime, InvokeTool, ModelDecision, ToolBinding

        sid = "authorization-restart"
        await self.host.create(sid, self.workspace.workspace_id)
        journal = SqliteJournal(self.store.require(sid))
        runtime = AgentRuntime(
            journal,
            SimpleNamespace(decide=AsyncMock(return_value=ModelDecision(command_requests=(
                InvokeTool("write_file", (("path", "probe.md"), ("content", "probe"))),
            )))),
            {"write_file": ToolBinding(AsyncMock(), requires_authorization=True)},
        )
        await runtime.receive_user_message(sid, "write probe", delivery_id="write")
        issued = await runtime.advance(sid)
        command_id = issued.step.commands[0].command_id
        await self.host.select("web:old", sid)
        self.assertEqual((await self.host.view(sid)).pending_authorization_ids, (command_id,))
        await self.host.close()
        self.host = self.new_host()
        channel = WebChannel(self.host, AssistantQueries(self.store, self.host))
        connection = channel.connect()
        recovered = await channel.select(connection.connection_id, sid)
        self.assertEqual(recovered.session.pending_authorization_ids, (command_id,))
        resolved = await channel.authorize_command(connection.connection_id, sid, command_id, False)
        self.assertEqual(resolved.session.pending_authorization_ids, ())
        self.assertEqual(resolved.items[1].tools[0].status, "rejected")
        self.assertFalse((self.root / "probe.md").exists())
        await until(lambda: (sid, "done") in self.output)
        self.assertTrue(self.host.failures.empty())

    async def test_user_image_refs_cross_the_worker_boundary(self):
        import json
        from PIL import Image
        from io import BytesIO
        from helperme.assistant.attachments import AttachmentGateway

        buffer = BytesIO()
        Image.new("RGB", (16, 16), "red").save(buffer, format="PNG")
        await self.host.create("image-session", self.workspace.workspace_id)
        ref = AttachmentGateway(self.home.runtime_sessions_root).for_session(
            "image-session"
        ).save_image(buffer.getvalue(), "image/png")
        await self.host.accept_input(
            "image-session",
            "[Image #1]",
            artifact_refs=(ref.attachment_id,),
            delivery_id="image-input",
        )
        await until(lambda: ("image-session", "done") in self.output)
        received = json.loads(
            (self.root / "received-images.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            received[0]["image_url"]["url"].startswith("data:image/png;base64,")
        )

    async def persist_child(self):
        from datetime import datetime, timezone
        from helperme.assistant.subagent.workspace import child_layout, workspace_versions
        from helperme.runtime.events import (
            DomainFactCommitted,
            EventDraft,
            DeliveryIdentity,
        )

        root, ref = child_layout(self.home, PARENT, CHILD)
        await workspace_versions(self.home, self.workspace).fork(root, ref)
        await self.store.create(CHILD, workspace_id=self.workspace.workspace_id)
        journal = SqliteJournal(self.store.require(CHILD))
        await journal.accept_delivery(
            EventDraft(
                event_id="task-event",
                session_id=CHILD,
                payload=DomainFactCommitted(
                    "subagent.task",
                    child_task("read")["data"],
                    requests_decision=True,
                ),
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity(
                    "subagent",
                    f"{MANUAL_CHILD_COMMAND}:task",
                ),
            )
        )
        return journal

    async def assert_failure_report(self, message):
        from helperme.runtime import DomainFactCommitted

        failure = await asyncio.wait_for(self.host.wait_failure(), 30)
        self.assertEqual(failure.failure.exception_type, "builtins.RuntimeError")
        self.assertIn(message, failure.failure.message)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        reports = [
            e.payload
            for e in events
            if isinstance(e.payload, DomainFactCommitted)
            and e.payload.fact_type == "subagent.report"
        ]
        self.assertEqual(len(reports), 1)
        self.assertIn(message, reports[0].data["failure"])

    async def test_failed_reader_still_reports_and_exits(self):
        from helperme.assistant.host.ipc import WorkerFailed

        await self.host.create("parent", self.workspace.workspace_id)
        await self.persist_child()
        self.host.config_factory = partial(failing_request_config, self.root)
        with self.assertRaises(WorkerFailed):
            await asyncio.wait_for(
                self.host.resolve_authorizations(CHILD, approved=True), 30
            )
        await self.assert_failure_report("application request failed")

    async def test_new_child_has_parent_identity_before_initialization(self):
        await self.host.create("parent", self.workspace.workspace_id)
        self.host.config_factory = partial(failing_startup_config, self.root, "config")
        await asyncio.wait_for(
            self.host._route(
                "create_child",
                CHILD,
                child_task("read"),
            ),
            30,
        )
        await self.assert_failure_report("config initialization failed")

    async def test_existing_session_cannot_be_adopted_as_a_child(self):
        await self.host.create(PARENT, self.workspace.workspace_id)
        await self.host.create(CHILD, self.workspace.workspace_id)

        with self.assertRaisesRegex(
            ValueError,
            "does not match delegate intent",
        ):
            await self.host._route(
                "create_child",
                CHILD,
                child_task("read"),
            )

        self.assertNotIn(CHILD, self.host.workers)

    async def test_sibling_creation_serializes_workspace_forks(self):
        from unittest.mock import AsyncMock, patch

        await self.store.create(PARENT, workspace_id=self.workspace.workspace_id)
        active = 0
        peak = 0

        class Versions:
            async def fork(self, root, _ref, *, conflict_from=None):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                root.mkdir(parents=True)
                active -= 1

        intents = [
            DelegateIntent(f"command-{index}", PARENT, f"{PARENT}/sub-command-{index}", "read")
            for index in range(3)
        ]
        with (
            patch("helperme.assistant.host.supervisor.workspace_versions", return_value=Versions()),
            patch.object(self.host, "request", new_callable=AsyncMock),
        ):
            await asyncio.gather(*(
                self.host._route("create_child", intent.child_session_id, task_fact_arguments(intent))
                for intent in intents
            ))
        self.assertEqual(peak, 1)
        self.assertTrue(all(self.store.path(intent.child_session_id).is_file() for intent in intents))

    async def test_child_startup_failure_does_not_strand_parent_delegate(self):
        from tests.fixtures.session_worker import delegate_startup_failure_config

        self.host.config_factory = partial(delegate_startup_failure_config, self.root)
        await self.host.create("parent", self.workspace.workspace_id)
        await self.host.receive_user_message(
            "parent", "DELEGATE_CHILDREN", delivery_id="input"
        )
        for _ in range(2):
            failure = await asyncio.wait_for(self.host.wait_failure(), 30)
            self.assertIn("/sub-", failure.session_id)
            self.assertIn("child initialization failed", failure.failure.message)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(len(project_delegations(events)), 2)
        self.assertEqual(len(project_reclaimed(events)), 2)
        self.assertTrue(self.host.failures.empty())
        self.assertIn(("parent", "done"), self.output)

    async def assert_startup_failure(self, stage):
        from helperme.assistant.host.ipc import WorkerFailed

        await self.host.create("parent", self.workspace.workspace_id)
        await self.persist_child()
        self.host.config_factory = partial(failing_startup_config, self.root, stage)
        with self.assertRaises(WorkerFailed):
            await asyncio.wait_for(self.host.resume(CHILD), 30)
        await self.assert_failure_report(f"{stage} initialization failed")

    async def test_config_initialization_failure_is_reported(self):
        await self.assert_startup_failure("config")

    async def test_assembly_initialization_failure_is_reported(self):
        await self.assert_startup_failure("assembly")

    async def test_client_initialization_failure_is_reported(self):
        await self.assert_startup_failure("client")

    async def asyncSetUp(self):
        from helperme.sandbox.registry import WorkspaceRegistry

        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = HelperMeHome(self.root / "home")
        self.store = SessionStore(self.home.runtime_sessions_root)
        self.workspace = WorkspaceRegistry.load(
            self.home.workspaces_path
        ).register_path(self.root)
        self.output = []
        self.output_ids = []
        self.previews = []
        self.tools = []
        self.delivery_order = []
        self.activities = []
        self.host = self.new_host()

    def new_host(self):
        def deliver(session_id, output_id, text):
            self.output.append((session_id, text))
            self.output_ids.append(output_id)
            self.delivery_order.append(("final", session_id, output_id, text))

        def preview(*values):
            self.previews.append(values)
            self.delivery_order.append(("preview", *values))

        def session_activity(session_id, activity):
            self.activities.append((session_id, activity))

        def tool_progress(*values):
            self.tools.append(values)

        return HostSupervisor(
            self.store,
            partial(config_for, self.root),
            self.home,
            deliver,
            llm=ProcessLlm(self.root),
            preview_sink=preview,
            session_activity_sink=session_activity,
            tool_progress_sink=tool_progress,
        )

    async def asyncTearDown(self):
        await self.host.close()
        self.directory.cleanup()

    async def test_idle_exit_delivery_and_explicit_restart(self):
        await self.host.create("one", self.workspace.workspace_id)
        await until(lambda: not self.host.workers and not self.host.watchers)
        await self.host.receive_user_message("one", "hello", delivery_id="input")
        await until(lambda: self.output)
        self.assertEqual(self.activities[0], ("one", "running"))
        await until(lambda: not self.host.workers and not self.host.watchers)
        await self.host.close()
        self.host = self.new_host()
        self.assertEqual(self.host.workers, {})
        view = await self.host.resume("one")
        self.assertEqual(view.status, "waiting")
        await self.host.receive_user_message("one", "hello", delivery_id="input")
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertEqual(self.output, [("one", "done")])

    async def test_preview_and_final_cross_worker_boundary_in_order(self):
        await self.host.create("one", self.workspace.workspace_id)
        await self.host.receive_user_message("one", "hello", delivery_id="input")
        await until(lambda: self.output == [("one", "done")])

        started, delta, final = self.delivery_order
        self.assertEqual(started[:3], ("preview", "one", "started"))
        self.assertEqual(delta[:3], ("preview", "one", "delta"))
        self.assertEqual(delta[4], "done")
        self.assertEqual(final[:2], ("final", "one"))
        self.assertEqual(started[3], delta[3])
        self.assertEqual(delta[3], final[2])

    async def test_worker_exit_closes_generation_preview_tools_and_activity(self):
        from tests.fixtures.session_worker import blocking_tool_config

        self.host.config_factory = partial(blocking_tool_config, self.root)
        await self.host.create("one", self.workspace.workspace_id)
        await self.host.receive_user_message(
            "one", "BLOCK_TOOL", delivery_id="input"
        )
        await until(
            lambda: (self.root / "tool-started").exists()
            and any(value[1] == "start" for value in self.tools)
        )
        worker = self.host.workers["one"]
        worker.process.terminate()

        failure = await asyncio.wait_for(self.host.wait_failure(), 30)
        self.assertEqual(failure.session_id, "one")
        await until(lambda: "one" not in self.host.workers)
        phases = [value[1] for value in self.previews]
        self.assertEqual(phases[0], "started")
        self.assertEqual(phases[-1], "aborted")
        self.assertEqual([value[1] for value in self.tools], ["start", "fail"])
        self.assertEqual(self.activities[-1], ("one", "idle"))

    async def test_selected_idle_worker_stays_until_owner_releases_it(self):
        await self.host.create("one", self.workspace.workspace_id)
        view = await self.host.select("cli", "one")
        self.assertEqual(view.status, "waiting")
        self.assertNotIn("one", self.host.workers)

        await self.host.receive_user_message("one", "hello", delivery_id="input")
        await until(lambda: self.output == [("one", "done")])
        await until(
            lambda: "one" in self.host.workers
            and self.host.workers["one"].idle_revision is not None
        )
        process_id = self.host.workers["one"].process.pid
        self.assertIn("one", self.host.workers)
        self.assertEqual(self.host.workers["one"].process.pid, process_id)

        await self.host.select("web", "one")
        await self.host.release("cli")
        self.assertIn("one", self.host.workers)
        await self.host.release("web")
        await until(lambda: "one" not in self.host.workers)

    async def test_decision_cancel_is_cooperative_and_durable_across_worker_boundary(self):
        from helperme.runtime import DecisionCancelled

        self.host.config_factory = partial(cancellable_config, self.root)
        self.host.llm = CancellableProcessLlm(self.root)
        await self.host.create("one", self.workspace.workspace_id)
        await self.host.select("acp", "one")
        await self.host.accept_input(
            "one",
            "CANCEL_PROCESS",
            delivery_id="input",
        )
        await until(lambda: (self.root / "cancel-started").exists())

        await self.host.cancel_turn("one")
        view = await self.host.wait_quiescent("one")

        self.assertTrue((self.root / "cancel-observed").exists())
        self.assertEqual(view.status, "waiting")
        events = await SqliteJournal(self.store.require("one")).snapshot("one")
        self.assertIsInstance(events[-1].payload, DecisionCancelled)

    async def test_unselected_busy_worker_stops_only_after_work_finishes(self):
        await self.host.create("one", self.workspace.workspace_id)
        await self.host.select("cli", "one")
        await self.host.receive_user_message(
            "one", "BLOCK_PROCESS", delivery_id="input"
        )
        await until(lambda: list(self.root.glob("blocked-*")))

        await self.host.release("cli")
        self.assertIn("one", self.host.workers)

        (self.root / "release").touch()
        await until(lambda: ("one", "done") in self.output)
        await until(lambda: "one" not in self.host.workers)

    async def test_failed_selection_keeps_previous_owner_mapping(self):
        from datetime import datetime, timezone

        from helperme.assistant.host.ipc import WorkerFailed
        from helperme.runtime import UserMessageReceived
        from helperme.runtime.events import DeliveryIdentity, EventDraft

        await self.host.create("old", self.workspace.workspace_id)
        await self.host.select("cli", "old")
        await self.host.create("broken", self.workspace.workspace_id)
        await SqliteJournal(self.store.require("broken")).accept_delivery(
            EventDraft(
                event_id="broken-input",
                session_id="broken",
                payload=UserMessageReceived("start"),
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity("test", "broken-input"),
            )
        )
        self.host.config_factory = partial(
            failing_startup_config, self.root, "config"
        )

        with self.assertRaises(WorkerFailed):
            await self.host.select("cli", "broken")

        self.assertEqual(self.host.selections["cli"], "old")
        self.assertNotIn("broken", self.host.workers)

    async def test_blocking_worker_and_crash_do_not_stop_another(self):
        await self.host.create("blocked", self.workspace.workspace_id)
        await self.host.receive_user_message(
            "blocked", "BLOCK_PROCESS", delivery_id="a"
        )
        await until(lambda: list(self.root.glob("blocked-*")))
        blocked_at = time.monotonic()
        await self.host.create("crash", self.workspace.workspace_id)
        await self.host.receive_user_message("crash", "CRASH_PROCESS", delivery_id="b")
        failure = await asyncio.wait_for(self.host.wait_failure(), 30)
        self.assertEqual(failure.failure.exception_type, "builtins.RuntimeError")
        self.assertIn("intentional worker crash", failure.failure.traceback)
        await self.host.create("healthy", self.workspace.workspace_id)
        await self.host.receive_user_message("healthy", "hello", delivery_id="c")
        await until(lambda: ("healthy", "done") in self.output)
        self.assertIn("blocked", self.host.workers)
        await asyncio.sleep(max(0, 31 - (time.monotonic() - blocked_at)))
        (self.root / "release").touch()
        await until(lambda: ("blocked", "done") in self.output)

    async def test_two_children_return_after_parent_worker_exits(self):
        await self.host.create("parent", self.workspace.workspace_id)
        await self.host.receive_user_message(
            "parent", "DELEGATE_CHILDREN", delivery_id="input"
        )
        await until(lambda: len(list(self.root.glob("blocked-*"))) == 2)
        await until(lambda: "parent" not in self.host.workers)
        pids = {worker.process.pid for worker in self.host.workers.values()}
        self.assertEqual(len(pids), 2)
        (self.root / "release").touch()
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(len(project_delegations(events)), 2)
        self.assertEqual(len(project_reclaimed(events)), 2)
        self.assertTrue(all(sid == "parent" for sid, _ in self.output))

    async def test_resume_selected_parent_recovers_its_children_only(self):
        await self.host.create("unrelated", self.workspace.workspace_id)
        await self.host.create("parent", self.workspace.workspace_id)
        await self.host.receive_user_message(
            "parent", "DELEGATE_CHILDREN", delivery_id="input"
        )
        await until(lambda: len(list(self.root.glob("blocked-*"))) == 2)
        await until(lambda: "parent" not in self.host.workers)
        await self.host.close()
        (self.root / "release").touch()
        self.host = self.new_host()
        self.assertEqual(self.host.workers, {})
        await asyncio.wait_for(self.host.resume("parent"), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(len(project_reclaimed(events)), 2)
        unrelated = await SqliteJournal(
            self.store.require("unrelated")
        ).snapshot("unrelated")
        self.assertEqual(len(project_delegations(unrelated)), 0)
        self.assertEqual(len(project_reclaimed(unrelated)), 0)
        self.assertTrue(self.host.failures.empty())

    async def test_parent_archive_removes_child_worktrees_after_stopping_workers(self):
        from helperme.assistant.subagent.workspace import child_layout

        await self.host.create("parent", self.workspace.workspace_id)
        await self.host.receive_user_message("parent", "DELEGATE_CHILDREN", delivery_id="input")
        await until(lambda: len(list(self.root.glob("blocked-*"))) == 2)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        children = project_delegations(events)
        roots = [child_layout(self.home, "parent", child)[0] for child in children]
        self.assertTrue(all(root.is_dir() for root in roots))
        await asyncio.wait_for(self.host.archive("parent"), 30)
        self.assertTrue(self.host.is_archived("parent"))
        self.assertTrue(all(not root.exists() for root in roots))
        self.assertTrue(all(child not in self.host.workers for child in children))

    async def test_saved_return_is_redelivered_after_host_restart(self):
        route = self.host._route

        async def interrupted_route(operation, session_id, arguments):
            if operation == "fact" and session_id == "parent":
                raise RuntimeError("interrupted before parent acceptance")
            return await route(operation, session_id, arguments)

        self.host._route = interrupted_route
        (self.root / "release").touch()
        await self.host.create("parent", self.workspace.workspace_id)
        await self.host.receive_user_message(
            "parent", "DELEGATE_CHILDREN", delivery_id="input"
        )
        await asyncio.wait_for(self.host.wait_failure(), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        await self.host.close()
        self.host = self.new_host()
        await asyncio.wait_for(self.host.resume("parent"), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(len(project_reclaimed(events)), 2)
        self.assertTrue(self.host.failures.empty())

    async def test_deliveries_during_idle_transition_are_all_durable(self):
        await self.host.create("one", self.workspace.workspace_id)
        await asyncio.gather(
            *(
                self.host.receive_user_message(
                    "one", f"message {i}", delivery_id=f"input-{i}"
                )
                for i in range(12)
            )
        )
        await until(lambda: not self.host.workers and not self.host.watchers)
        from helperme.runtime import UserMessageReceived

        events = await SqliteJournal(self.store.require("one")).snapshot("one")
        self.assertEqual(
            len([e for e in events if isinstance(e.payload, UserMessageReceived)]), 12
        )
        self.assertTrue(self.host.failures.empty())

    async def test_child_recovery_preserves_unfinished_read_and_reports_once(self):
        from helperme.runtime import DispatchAttemptStarted

        self.host.config_factory = partial(interrupted_read_config, self.root)
        (self.root / "release").touch()
        await self.host.create("parent", self.workspace.workspace_id)
        await self.host._route(
            "create_child",
            CHILD,
            child_task("READ_THEN_REPORT"),
        )
        failure = await asyncio.wait_for(self.host.wait_failure(), 30)
        self.assertEqual(failure.failure.exception_type, "ProcessExit")
        self.assertIn("exit code 1", failure.failure.message)
        await until(lambda: not self.host.workers and not self.host.watchers)
        journal = SqliteJournal(self.store.require(CHILD))
        before = await journal.snapshot(CHILD)
        started = next(
            e.event_id for e in before if isinstance(e.payload, DispatchAttemptStarted)
        )
        await asyncio.wait_for(self.host.resume(CHILD), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        after = await journal.snapshot(CHILD)
        self.assertEqual(after[:len(before)], before)
        self.assertIn(started, {e.event_id for e in after})
        self.assertFalse((self.root / "read-retried").exists())
        from helperme.runtime import CommandPhase, DomainFactCommitted, StateProjector
        from helperme.assistant.subagent.subagent import REPORT_FACT, RETURN_FACT
        state = StateProjector().project(CHILD, after).state
        self.assertEqual(state.commands[0].phase, CommandPhase.UNKNOWN)
        self.assertEqual(sum(
            isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == RETURN_FACT
            for e in after
        ), 1)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(project_reclaimed(events), frozenset({CHILD}))
        report = next(e for e in events if isinstance(e.payload, DomainFactCommitted)
                      and e.payload.fact_type == REPORT_FACT)
        self.assertTrue(report.payload.requests_decision)
        self.assertIn("执行被中断", report.payload.data["failure"])
        self.assertIn(state.commands[0].command.command_id, report.payload.data["failure"])
        await asyncio.wait_for(self.host.resume(CHILD), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertEqual(await journal.snapshot(CHILD), after)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(sum(
            isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == REPORT_FACT
            for e in events
        ), 1)
        self.assertTrue(self.host.failures.empty())

    async def test_child_unexpected_crash_is_reported_then_still_exposed(self):
        from helperme.runtime import DomainFactCommitted
        from helperme.assistant.subagent.subagent import REPORT_FACT

        await self.host.create("parent", self.workspace.workspace_id)
        await self.host._route(
            "create_child",
            CHILD,
            child_task("CRASH_PROCESS"),
        )
        failure = await asyncio.wait_for(self.host.wait_failure(), 30)
        self.assertIn("intentional worker crash", failure.failure.message)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(project_reclaimed(events), frozenset({CHILD}))
        report = next(
            event.payload
            for event in events
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == REPORT_FACT
        )
        self.assertIs(report.data["reported"], False)
        self.assertIn("intentional worker crash", report.data["failure"])
        self.assertIn("Traceback", report.data["failure"])

    async def test_parent_reclaim_stops_child_and_does_not_resume_it(self):
        from helperme.runtime import DomainFactCommitted
        from helperme.assistant.subagent.subagent import REPORT_FACT, RETURN_FACT

        await self.host.create("parent", self.workspace.workspace_id)
        await self.host._route(
            "create_child",
            CHILD,
            child_task("read something"),
        )
        await until(lambda: list(self.root.glob("blocked-*")))
        await self.host._route(
            "reclaim_child",
            CHILD,
            {"parent_session_id": "parent", "reason": "stop"},
        )
        await until(lambda: CHILD not in self.host.workers)
        await until(lambda: not self.host.watchers or CHILD not in self.host.workers)
        self.assertTrue(self.host.failures.empty())

        child_events = await SqliteJournal(self.store.require(CHILD)).snapshot(
            CHILD
        )
        returned = next(
            event.payload
            for event in child_events
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == RETURN_FACT
        )
        self.assertIs(returned.data["cancelled"], True)
        self.assertEqual(returned.data["reason"], "stop")

        parent_events = await SqliteJournal(self.store.require("parent")).snapshot(
            "parent"
        )
        report = next(
            event.payload
            for event in parent_events
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == REPORT_FACT
        )
        self.assertIs(report.requests_decision, False)
        self.assertIs(report.data["cancelled"], True)
        self.assertEqual(project_reclaimed(parent_events), frozenset({CHILD}))

        await asyncio.wait_for(self.host.resume("parent"), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertNotIn(CHILD, self.host.workers)
        self.assertTrue(self.host.failures.empty())
