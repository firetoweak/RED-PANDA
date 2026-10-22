from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
import tempfile
import time
import unittest

import pytest

from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.subagent.subagent import (
    DelegateIntent,
    project_delegations,
    project_reclaimed,
    task_fact_arguments,
)
from redpanda.paths import RedPandaHome
from redpanda.runtime import SqliteJournal
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
    async def test_large_cli_catalog_does_not_crash_worker_or_hide_registered_clis(self):
        from dataclasses import replace

        from redpanda.assistant.catalog import project_catalog
        from redpanda.cli.registry import CliRegistry
        from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
        from redpanda.runtime import CommandOutcomeReceived
        from tests.cli.test_runtime import make_record

        registry = CliRegistry(self.home.clis_root)
        names = {f"cli-{index:02}" for index in range(40)}
        for name in sorted(names):
            await registry.add(replace(make_record(name), description="x" * 1000))

        class LoadLastCli(ProcessLlm):
            calls = 0

            async def chat(client, messages, model, **kwargs):
                client.calls += 1
                if client.calls == 1:
                    return LLMCallResult(LLMResponse(calls=(ToolCall(
                        "last-cli", "load_cli", '{"cli_id":"cli-39"}',
                    ),)), LLMUsage(input_tokens=10, output_tokens=5))
                return LLMCallResult(LLMResponse(content="done"), LLMUsage(input_tokens=10, output_tokens=5))

        self.host.llm = LoadLastCli(self.root)
        sid = "large-cli-catalog"
        await self.host.create(sid, self.workspace.workspace_id)
        await self.host.select("cli-page", sid)
        await self.host.accept_input(sid, "inspect the last CLI", delivery_id="inspect")
        await asyncio.wait_for(self.host.wait_quiescent(sid), 30)
        events = await SqliteJournal(self.store.require(sid)).snapshot(sid)
        catalog = project_catalog(events)
        self.assertEqual({item.id for item in catalog.clis}, names)
        self.assertIn("load_cli", catalog.clis[-1].description)
        loaded = next(event.payload.outcome.value for event in events
                      if isinstance(event.payload, CommandOutcomeReceived)
                      and event.payload.outcome.value["code"] == "CLI_LOADED")
        self.assertEqual(loaded["data"]["cli_id"], "cli-39")
        self.assertEqual(loaded["data"]["description"], "x" * 1000)
        self.assertIn((sid, "done"), self.output)
        self.assertTrue(self.host.failures.empty())

        # Later catalog updates must remain usable while the directory is over budget.
        await registry.add(replace(make_record("new-cli"), description="y" * 1000))
        await self.host.accept_input(sid, "refresh the CLI directory", delivery_id="refresh")
        await asyncio.wait_for(self.host.wait_quiescent(sid), 30)
        updated = await SqliteJournal(self.store.require(sid)).snapshot(sid)
        self.assertEqual({item.id for item in project_catalog(updated).clis}, names | {"new-cli"})
        self.assertEqual(self.output.count((sid, "done")), 2)
        self.assertIsNone(self.host.current_failure(sid))
        self.assertTrue(self.host.failures.empty())

    async def test_web_can_reject_journal_authorization_after_host_restart(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from redpanda.assistant.conversations import AssistantQueries
        from redpanda.channels.web.channel import WebChannel
        from redpanda.runtime import AgentRuntime, InvokeTool, ModelDecision, ToolBinding

        sid = "authorization-restart"
        await self.host.create(sid, self.workspace.workspace_id)
        # This scenario exercises manual rejection, including its policy after restart.
        await self.host.set_auto_authorize(False)
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
        self.assertFalse(recovered.session.auto_authorize)
        await self.host.create("another-after-restart", self.workspace.workspace_id)
        self.assertFalse((await self.host.view("another-after-restart")).auto_authorize)
        self.assertEqual(recovered.session.pending_authorization_ids, (command_id,))
        resolved = await channel.authorize_command(connection.connection_id, sid, command_id, False)
        self.assertEqual(resolved.session.pending_authorization_ids, ())
        self.assertEqual(resolved.items[1].tools[0].status, "rejected")
        self.assertFalse((self.root / "probe.md").exists())
        await until(lambda: (sid, "done") in self.output)
        self.assertTrue(self.host.failures.empty())

    async def persist_child(self):
        from datetime import datetime, timezone
        from redpanda.runtime.events import (
            DomainFactCommitted,
            EventDraft,
            DeliveryIdentity,
        )

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
        from redpanda.runtime import DomainFactCommitted

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
        from redpanda.assistant.host.ipc import WorkerFailed

        await self.host.create("parent", self.workspace.workspace_id)
        await self.persist_child()
        self.host.config_factory = partial(failing_request_config, self.root)
        with self.assertRaises(WorkerFailed):
            await asyncio.wait_for(
                self.host.resolve_authorizations(CHILD, approved=True), 30
            )
        await self.assert_failure_report("application request failed")

    async def test_invalid_authorization_requests_do_not_fail_worker(self):
        from redpanda.assistant.authorization import (
            AuthorizationConflict,
            AuthorizationTargetNotFound,
        )

        sid = "authorization-errors"
        await self.host.create(sid, self.workspace.workspace_id)
        await self.host.view(sid)
        before = await SqliteJournal(self.store.require(sid)).snapshot(sid)
        for approved in (False, True):
            with self.assertRaises(AuthorizationTargetNotFound):
                await asyncio.wait_for(self.host.resolve_authorization(
                    sid, "missing", approved=approved,
                ), 30)
            with self.assertRaises(AuthorizationConflict):
                await asyncio.wait_for(self.host.resolve_authorizations(
                    sid, approved=approved,
                ), 30)
        self.assertEqual(await SqliteJournal(self.store.require(sid)).snapshot(sid), before)
        self.assertTrue(self.host.failures.empty())
        await self.host.accept_input(sid, "hello", delivery_id="hello")
        await until(lambda: (sid, "done") in self.output)
        self.assertTrue(self.host.failures.empty())

    async def test_conflicting_input_replay_preserves_worker_and_original_message(self):
        from redpanda.assistant.inputs import InputConflict
        from redpanda.runtime import UserMessageReceived

        sid = "input-replay"
        await self.host.create(sid, self.workspace.workspace_id)
        await self.host.select("web:replay", sid)
        await self.host.accept_input(sid, "original", delivery_id="input-1", source="web")
        await asyncio.wait_for(self.host.wait_quiescent(sid), 30)
        journal = SqliteJournal(self.store.require(sid))
        before = await journal.snapshot(sid)
        worker = self.host.workers[sid]
        for operation in (self.host.accept_input, self.host.receive_user_message):
            await operation(sid, "original", delivery_id="input-1", source="web")
            await asyncio.wait_for(self.host.wait_quiescent(sid), 30)
            with self.assertRaises(InputConflict):
                await asyncio.wait_for(operation(
                    sid, "changed", delivery_id="input-1", source="web",
                ), 30)
            self.assertEqual(await journal.snapshot(sid), before)
            self.assertIs(self.host.workers[sid], worker)
            self.assertTrue(worker.process.is_alive())
            self.assertIsNone(self.host.current_failure(sid))
            self.assertTrue(self.host.failures.empty())
        await self.host.accept_input(sid, "next", delivery_id="input-2", source="web")
        await asyncio.wait_for(self.host.wait_quiescent(sid), 30)
        events = await journal.snapshot(sid)
        self.assertEqual([e.payload.content for e in events if isinstance(e.payload, UserMessageReceived)],
                         ["original", "next"])
        self.assertEqual(self.output.count((sid, "done")), 2)
        self.assertTrue(self.host.failures.empty())

    async def test_child_startup_failure_does_not_strand_parent_delegate(self):
        from tests.fixtures.session_worker import delegate_startup_failure_config

        self.host.config_factory = partial(delegate_startup_failure_config, self.root)
        await self.host.create("parent", self.workspace.workspace_id)
        await self.host.receive_user_message(
            "parent", "DELEGATE_CHILDREN", delivery_id="input"
        )
        for _ in range(2):
            failure = await asyncio.wait_for(self.host.wait_failure(), 30)
            self.assertIn("/sub-", failure.session_id, failure.failure.render())
            self.assertIn("child initialization failed", failure.failure.message)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(len(project_delegations(events)), 2)
        self.assertEqual(len(project_reclaimed(events)), 2)
        self.assertTrue(self.host.failures.empty())
        self.assertIn(("parent", "done"), self.output)

    async def assert_startup_failure(self, stage):
        from redpanda.assistant.host.ipc import WorkerFailed

        await self.host.create("parent", self.workspace.workspace_id)
        await self.persist_child()
        self.host.config_factory = partial(failing_startup_config, self.root, stage)
        with self.assertRaises(WorkerFailed):
            await asyncio.wait_for(self.host.resume(CHILD), 30)
        await self.assert_failure_report(f"{stage} initialization failed")

    async def test_config_initialization_failure_is_reported(self):
        await self.assert_startup_failure("config")

    async def asyncSetUp(self):
        from redpanda.filesystem.registry import WorkspaceRegistry

        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "project"
        self.root.mkdir()
        self.home = RedPandaHome(Path(self.directory.name) / "home")
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

    async def test_close_rejects_worker_start_waiting_for_journal(self):
        from unittest.mock import patch

        await self.host.create("one", self.workspace.workspace_id)
        entered, release = asyncio.Event(), asyncio.Event()
        snapshot = SqliteJournal.snapshot

        async def held_snapshot(journal, session_id):
            events = await snapshot(journal, session_id)
            entered.set()
            await release.wait()
            return events

        with patch.object(SqliteJournal, "snapshot", held_snapshot):
            starting = asyncio.create_task(self.host.request("view", "one", {}))
            self.host._track("one", starting)
            await asyncio.wait_for(entered.wait(), 30)
            closing = asyncio.create_task(self.host.close())
            await until(lambda: self.host.closed)
            release.set()
            await asyncio.wait_for(closing, 30)
            try:
                with self.assertRaisesRegex(RuntimeError, "Host closed"):
                    await starting
                self.assertFalse(self.host.workers)
                self.assertFalse(self.host.watchers)
            finally:
                await self.host.close()

    async def test_model_failure_survives_query_and_selection_then_retries_without_repeating_tool(self):
        from redpanda.assistant.conversations import AssistantQueries
        from redpanda.llm.api import LLMAuthenticationError, LLMTransientError, LLMProviderError, LLMCallResult, LLMResponse, LLMUsage, ToolCall
        from redpanda.runtime.events import UserMessageReceived, CommandOutcomeReceived, StepCommitted

        (self.root / "input.txt").write_text("evidence", encoding="utf-8")
        for index, error in enumerate((LLMAuthenticationError("401"), LLMTransientError("429 exhausted"),
                                       LLMProviderError("500"), LLMTransientError("stream interrupted"))):
            with self.subTest(error=str(error)):
                class FailAfterRead(ProcessLlm):
                    count = 0

                    async def chat(client, messages, model, **kwargs):
                        client.count += 1
                        if client.count == 1:
                            return LLMCallResult(LLMResponse(calls=(ToolCall("read-evidence", "read_file", '{"path":"input.txt"}'),)),
                                                 LLMUsage(input_tokens=10, output_tokens=5))
                        if client.count == 2:
                            raise error
                        return await super().chat(messages, model, **kwargs)

                self.host.llm = llm = FailAfterRead(self.root)
                session_id = f"failed-model-{index}"
                await self.host.create(session_id, self.workspace.workspace_id)
                await self.host.accept_input(session_id, "read", delivery_id=f"input-{index}")
                await until(lambda: self.host.current_failure(session_id) is not None)
                await until(lambda: not self.host.workers and not self.host.watchers)
                queries = AssistantQueries(self.store, self.host)
                refreshed = await queries.conversation(session_id)
                self.assertIn("认证失败" if index == 0 else str(error), refreshed.session.last_error)
                self.assertEqual(refreshed.session.status, "runnable")
                self.assertEqual(self.host.activity(session_id), "idle")
                before = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
                await self.host.select(f"viewer-{index}", session_id)
                self.assertNotIn(session_id, self.host.workers)
                self.assertEqual(llm.count, 2)
                self.assertIsNotNone(self.host.current_failure(session_id))
                await self.host.retry(session_id)
                await until(lambda: (session_id, "done") in self.output)
                await self.host.release(f"viewer-{index}")
                await until(lambda: not self.host.workers and not self.host.watchers)
                self.assertEqual(llm.count, 3)
                self.assertIsNone((await queries.conversation(session_id)).session.last_error)
                events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
                self.assertEqual(events[:len(before)], before)
                self.assertEqual(sum(isinstance(event.payload, UserMessageReceived) for event in events), 1)
                read_commands = {command.command_id for event in events
                                 if isinstance(event.payload, StepCommitted)
                                 for command in event.payload.step.commands if command.effect.name == "read_file"}
                self.assertEqual(len(read_commands), 1)
                self.assertEqual(sum(isinstance(event.payload, CommandOutcomeReceived)
                                     and event.payload.command_id in read_commands for event in events), 1)

    async def test_child_stream_crosses_worker_boundary_only_to_observer(self):
        from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall

        observed, thoughts = [], []

        class StreamingChildLlm(ProcessLlm):
            async def chat(client, messages, model, *, tools=None,
                           on_content_delta=None, on_reasoning_delta=None):
                if "report" not in {tool["function"]["name"] for tool in tools}:
                    return await super().chat(
                        messages, model, tools=tools,
                        on_content_delta=on_content_delta,
                        on_reasoning_delta=on_reasoning_delta,
                    )
                await on_reasoning_delta("child reasoning")
                await on_content_delta("child progress")
                return LLMCallResult(
                    LLMResponse(content="child progress", message_extensions={"reasoning_content": "child reasoning"},
                                calls=(ToolCall("report-one", "report", '{"summary":"child done"}'),)),
                    LLMUsage(input_tokens=10, output_tokens=5),
                )

        self.host.llm = StreamingChildLlm(self.root)
        self.host.subagent_output_sink = lambda *values: observed.append(values)
        self.host.thinking_sink = lambda *values: thoughts.append(values)
        await self.host.create(PARENT, self.workspace.workspace_id)
        await self.persist_child()
        await self.host.resume(CHILD)
        await until(lambda: (observed and (PARENT, "done") in self.output)
                    or not self.host.failures.empty())
        self.assertTrue(self.host.failures.empty())
        await until(lambda: not self.host.workers and not self.host.watchers)

        child_final, = observed
        self.assertEqual((child_final[0], child_final[2]), (CHILD, "child progress"))
        self.assertEqual(self.output, [(PARENT, "done")])
        child_delta, = [value for value in self.previews
                        if value[0] == CHILD and value[1] == "delta"]
        child_thought, = [value for value in thoughts
                          if value[0] == CHILD and value[1] == "delta"]
        self.assertEqual(child_delta[2:4], (child_final[1], "child progress"))
        self.assertEqual(child_thought[2:4], (child_final[1], "child reasoning"))
        self.assertTrue(self.host.failures.empty())

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

    async def test_decision_cancel_is_cooperative_and_durable_across_worker_boundary(self):
        from redpanda.runtime import DecisionCancelled

        self.host.config_factory = partial(cancellable_config, self.root)
        self.host.llm = CancellableProcessLlm(self.root)
        await self.host.create("one", self.workspace.workspace_id)
        await self.host.select("web", "one")
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

    async def test_cancel_parent_reclaims_all_children_and_later_input_can_continue(self):
        from redpanda.assistant.subagent.subagent import project_pending, project_returned

        await self.host.create(PARENT, self.workspace.workspace_id)
        await self.host.select("web", PARENT)
        await self.host.accept_input(PARENT, "DELEGATE_CHILDREN", delivery_id="delegate")
        await until(lambda: len(list(self.root.glob("blocked-*"))) == 2)
        before = await SqliteJournal(self.store.require(PARENT)).snapshot(PARENT)
        children = project_pending(before)
        self.assertEqual(len(children), 2)

        view = await self.host.cancel_turn(PARENT)

        self.assertTrue(view.paused)
        self.assertFalse(view.has_active_subagents)
        self.assertEqual(self.host.activity(PARENT), "idle")
        for child in children:
            self.assertNotIn(child, self.host.workers)
            child_events = await SqliteJournal(self.store.require(child)).snapshot(child)
            self.assertTrue(project_returned(child_events))
            self.assertTrue(child_events[-1].payload.data["cancelled"])
        # Viewing/resuming a stopped parent must not turn reclaim into new work.
        stopped_outputs = list(self.output)
        await self.host.resume(PARENT)
        await self.host.wait_quiescent(PARENT)
        self.assertEqual(self.output, stopped_outputs)
        await self.host.accept_input(PARENT, "继续新的要求", delivery_id="next")
        await until(lambda: len(self.output) == len(stopped_outputs) + 1)
        self.assertFalse(self.host.is_paused(PARENT))
        self.assertTrue(self.host.failures.empty())

    async def test_interrupt_input_cancels_old_decision_and_replay_preserves_new_work(self):
        from redpanda.assistant.inputs import InputConflict
        from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage
        from redpanda.runtime import DecisionCancelled, UserMessageReceived

        started, cancelled, redirected, release = (asyncio.Event() for _ in range(4))

        class RedirectLlm(ProcessLlm):
            calls = 0

            async def chat(client, messages, model, **kwargs):
                client.calls += 1
                if client.calls == 1:
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                redirected.set()
                await release.wait()
                return LLMCallResult(LLMResponse(content="按新指令完成"), LLMUsage(input_tokens=10, output_tokens=5))

        self.host.llm = client = RedirectLlm(self.root)
        sid = "interrupt-input"
        await self.host.create(sid, self.workspace.workspace_id)
        await self.host.select("web:interrupt", sid)
        await self.host.accept_input(sid, "旧指令", delivery_id="old", source="web")
        await asyncio.wait_for(started.wait(), 30)

        await self.host.accept_input(sid, "改为新指令", delivery_id="new", source="web", interrupt=True)
        await asyncio.wait_for(redirected.wait(), 30)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.host.is_paused(sid))
        await self.host.accept_input(sid, "改为新指令", delivery_id="new", source="web", interrupt=True)
        with self.assertRaises(InputConflict):
            await self.host.accept_input(sid, "不同内容", delivery_id="new", source="web", interrupt=True)
        self.assertEqual(client.calls, 2)
        release.set()
        await asyncio.wait_for(self.host.wait_quiescent(sid), 30)
        self.assertIn((sid, "按新指令完成"), self.output)
        events = await SqliteJournal(self.store.require(sid)).snapshot(sid)
        self.assertEqual([event.payload.content for event in events if isinstance(event.payload, UserMessageReceived)],
                         ["旧指令", "改为新指令"])
        self.assertEqual(sum(isinstance(event.payload, DecisionCancelled) for event in events), 1)
        self.assertTrue(self.host.failures.empty())

    async def test_interrupt_input_waits_for_children_and_serializes_repeated_send(self):
        from unittest.mock import AsyncMock

        await self.host.create(PARENT, self.workspace.workspace_id)
        await self.host.accept_input(PARENT, "DELEGATE_CHILDREN", delivery_id="delegate")
        await until(lambda: len(list(self.root.glob("blocked-*"))) == 2)
        previous_output_count = len(self.output)
        entered, release = asyncio.Event(), asyncio.Event()
        reclaim = self.host._reclaim_child

        async def held_reclaim(*args):
            entered.set()
            await release.wait()
            await reclaim(*args)

        self.host._reclaim_child = AsyncMock(side_effect=held_reclaim)
        stopping = asyncio.create_task(
            self.host.accept_input(PARENT, "新的独立输入", delivery_id="next", interrupt=True)
        )
        await asyncio.wait_for(entered.wait(), 30)
        incoming = asyncio.create_task(
            self.host.accept_input(PARENT, "新的独立输入", delivery_id="next", interrupt=True)
        )
        await asyncio.sleep(0)
        self.assertFalse(incoming.done())
        self.assertTrue(self.host.is_paused(PARENT))
        release.set()
        await asyncio.wait_for(asyncio.gather(stopping, incoming), 30)
        await until(lambda: len(self.output) == previous_output_count + 1)
        self.assertFalse(self.host.is_paused(PARENT))
        self.assertTrue(self.host.failures.empty())

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

        from redpanda.assistant.host.ipc import WorkerFailed
        from redpanda.runtime import UserMessageReceived
        from redpanda.runtime.events import DeliveryIdentity, EventDraft

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

    async def test_child_recovery_preserves_unfinished_read_and_reports_once(self):
        from redpanda.runtime import DispatchAttemptStarted

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
        from redpanda.runtime import CommandPhase, DomainFactCommitted, StateProjector
        from redpanda.assistant.subagent.subagent import REPORT_FACT, RETURN_FACT
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
        self.assertIn("被中断", report.payload.data["failure"])
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
        from redpanda.runtime import DomainFactCommitted
        from redpanda.assistant.subagent.subagent import REPORT_FACT

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
        from redpanda.runtime import DomainFactCommitted
        from redpanda.assistant.subagent.subagent import REPORT_FACT, RETURN_FACT

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
