from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from redpanda.assistant.artifacts import MemoryArtifactGateway
from redpanda.assistant.context.projection import ModelContextSettings
from redpanda.assistant.management import ManagementSurface
from redpanda.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from redpanda.assistant.runner import SessionNotFoundError
from redpanda.assistant.sessions import AssistantSessions, SessionView
from redpanda.assistant.toolsets import (
    LOAD_TOOLSET,
    ToolSurface,
    load_toolset_binding,
)
from redpanda.runtime import (
    AgentRuntime,
    CommandPhase,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    ToolBinding,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.assistant.test_toolsets import FakeEchoProvider, _schema_names
from tests.session_scheduler import RecordingScheduler, settle_session


class RegistryCatalog:
    def __init__(self, surface: ToolSurface) -> None:
        self.surface = surface

    def rehydrate(self, session_id, _events):
        self.surface.apply_catalog(
            session_id,
            self.surface.registry_descriptors(),
        )

    async def sync(self, _runtime, session_id):
        self.rehydrate(session_id, ())


class AssistantSessionResumeTest(unittest.IsolatedAsyncioTestCase):
    SESSION_ID = "resume-session"

    def _sessions(
        self,
        runtime: AgentRuntime,
        scheduler: RecordingScheduler,
        surface: ToolSurface | None = None,
    ) -> tuple[AssistantSessions, ToolSurface]:
        if surface is None:
            surface = ToolSurface()
        surface.attach(runtime)
        return (
            AssistantSessions(
                runtime,
                surface,
                scheduler,
                control=scheduler._control,
                management=ManagementSurface(
                    (),
                    MemoryArtifactGateway(),
                    ModelContextSettings(),
                ),
                catalog=RegistryCatalog(surface),
            ),
            surface,
        )

    async def test_resume_rejects_unknown_session(self):
        runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker(()), {})
        scheduler = RecordingScheduler(runtime, self.SESSION_ID)
        sessions, _surface = self._sessions(runtime, scheduler)
        try:
            with self.assertRaises(SessionNotFoundError):
                await sessions.resume("missing")
            self.assertFalse(await runtime.session_exists("missing"))
            self.assertEqual(scheduler.woken, [])
        finally:
            await scheduler.close()

    async def test_resume_rebuilds_toolset_projection_without_waking_idle_session(
        self,
    ):
        delivered: list[str] = []
        journal = MemoryJournal()
        surface = ToolSurface(providers=(FakeEchoProvider(),))
        runtime = AgentRuntime(
            journal,
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="loading",
                        command_requests=(
                            InvokeTool(LOAD_TOOLSET, (("toolset_id", "demo"),)),
                        ),
                    ),
                    lambda _frame: ModelDecision(
                        content="done",
                        command_requests=(
                            InvokeTool(DELIVER_TOOL_NAME, (("output_id", "output-1"), ("text", "done"))),
                        ),
                    ),
                )
            ),
            {
                **load_toolset_binding(surface),
                **deliver_binding(lambda _session_id, _output_id, text: delivered.append(text)),
            },
            SequentialIds(),
        )
        surface.attach(runtime)
        surface.apply_catalog(self.SESSION_ID, surface.registry_descriptors())
        await runtime.create_session(self.SESSION_ID)
        await runtime.receive_user_message(
            self.SESSION_ID,
            "load echo",
            delivery_id="load-1",
        )
        await settle_session(runtime, self.SESSION_ID)

        restored_surface = ToolSurface(providers=(FakeEchoProvider(),))
        restored_runtime = AgentRuntime(
            journal,
            ScriptedDecisionMaker(()),
            {
                **load_toolset_binding(restored_surface),
                **deliver_binding(lambda _session_id, _output_id, text: delivered.append(text)),
            },
            SequentialIds(),
        )
        scheduler = RecordingScheduler(restored_runtime, self.SESSION_ID)
        sessions, _surface = self._sessions(
            restored_runtime,
            scheduler,
            restored_surface,
        )
        try:
            view = await sessions.resume(self.SESSION_ID)
            self.assertEqual(
                _schema_names(restored_surface.schemas(self.SESSION_ID)),
                {LOAD_TOOLSET, "demo_ping"},
            )
            self.assertFalse(view.should_wake)
            self.assertEqual(view.status, RuntimeStatus.WAITING.value)
            self.assertEqual(scheduler.woken, [])
        finally:
            await scheduler.close()

    async def test_resume_wakes_runnable_user_message(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((lambda _frame: ModelDecision(content="ok"),)),
            {},
            SequentialIds(),
        )
        await runtime.create_session(self.SESSION_ID)
        await runtime.receive_user_message(
            self.SESSION_ID,
            "hello",
            delivery_id="user-1",
        )
        scheduler = RecordingScheduler(runtime, self.SESSION_ID)
        sessions, _surface = self._sessions(runtime, scheduler)
        try:
            view = await sessions.resume(self.SESSION_ID)
            self.assertTrue(view.should_wake)
            self.assertEqual(view.status, RuntimeStatus.RUNNABLE.value)
            self.assertEqual(scheduler.woken, [self.SESSION_ID])
            await scheduler.join()
            self.assertEqual(len((await runtime.state(self.SESSION_ID)).steps), 1)
        finally:
            await scheduler.close()

    async def test_resume_wakes_pending_authorization_without_dispatching(self):
        started = []

        async def transfer(_context, _arguments):
            started.append(1)
            return "sent"

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(InvokeTool("transfer"),),
                    ),
                )
            ),
            {"transfer": ToolBinding(transfer, requires_authorization=True)},
            SequentialIds(),
        )
        await runtime.create_session(self.SESSION_ID)
        await runtime.receive_user_message(
            self.SESSION_ID,
            "send money",
            delivery_id="ask-1",
        )
        await runtime.advance(self.SESSION_ID)
        state = await runtime.state(self.SESSION_ID)
        self.assertEqual(state.commands[0].phase, CommandPhase.PENDING)
        self.assertEqual(state.status, RuntimeStatus.WAITING)

        scheduler = RecordingScheduler(runtime, self.SESSION_ID)
        sessions, _surface = self._sessions(runtime, scheduler)
        try:
            view = await sessions.resume(self.SESSION_ID)
            self.assertTrue(view.should_wake)
            self.assertEqual(scheduler.woken, [self.SESSION_ID])
            await scheduler.join()
            restored = await runtime.state(self.SESSION_ID)
            self.assertEqual(restored.commands[0].phase, CommandPhase.PENDING)
            self.assertEqual(started, [])
        finally:
            await scheduler.close()

    async def test_resume_does_not_wake_unknown_attempt(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(InvokeTool("explode"),),
                    ),
                )
            ),
            {"explode": ToolBinding(_explode)},
            SequentialIds(),
        )
        scheduler = RecordingScheduler(runtime, self.SESSION_ID)
        sessions, _surface = self._sessions(runtime, scheduler)
        await sessions.create(self.SESSION_ID)
        try:
            await sessions.receive_user_message(
                self.SESSION_ID,
                "go",
                delivery_id="user-1",
            )
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await scheduler.join()
            wakes_before_resume = list(scheduler.woken)
            view = await sessions.resume(self.SESSION_ID)
            self.assertEqual(
                (await runtime.state(self.SESSION_ID)).commands[0].phase,
                CommandPhase.UNKNOWN,
            )
            self.assertFalse(view.should_wake)
            self.assertEqual(scheduler.woken, wakes_before_resume)
        finally:
            await scheduler.close()

    async def test_resume_does_not_wake_idle_session(self):
        runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker(()), {})
        scheduler = RecordingScheduler(runtime, self.SESSION_ID)
        sessions, _surface = self._sessions(runtime, scheduler)
        try:
            await sessions.create(self.SESSION_ID)
            view = await sessions.resume(self.SESSION_ID)
            self.assertFalse(view.should_wake)
            self.assertEqual(view.status, RuntimeStatus.WAITING.value)
            self.assertEqual(scheduler.woken, [])
        finally:
            await scheduler.close()

    async def test_create_and_policy_do_not_write_host_preference_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker(()), {})
            scheduler = RecordingScheduler(runtime, self.SESSION_ID)
            surface = ToolSurface()
            sessions = AssistantSessions(
                runtime,
                surface,
                scheduler,
                control=scheduler._control,
                management=ManagementSurface(
                    (),
                    MemoryArtifactGateway(),
                    ModelContextSettings(),
                ),
                catalog=RegistryCatalog(surface),
            )
            try:
                view = await sessions.create(self.SESSION_ID)
                self.assertFalse(view.auto_authorize)
                self.assertFalse(view.paused)
                updated = await sessions.apply_auto_authorize(
                    self.SESSION_ID,
                    enabled=True,
                )
                self.assertTrue(updated.auto_authorize)
                self.assertFalse((root / "auto_authorize.json").is_file())
                self.assertFalse((root / "paused.json").is_file())
            finally:
                await scheduler.close()


class AcceptInputTest(unittest.IsolatedAsyncioTestCase):
    async def test_control_confirmation_precedes_command_authorization(self):
        sessions = object.__new__(AssistantSessions)
        pending = SessionView(
            status="waiting",
            waiting_for=(),
            pending_authorization_ids=("command-1",),
            should_wake=False,
            control_approval=SimpleNamespace(request_id="request-1"),
        )
        resolved = SessionView(
            status="waiting",
            waiting_for=("external_fact",),
            pending_authorization_ids=("command-1",),
            should_wake=False,
            control_message="控制操作完成",
        )
        sessions.view = AsyncMock(side_effect=(pending, resolved))
        sessions.resolve_control = AsyncMock(return_value="控制操作完成")
        sessions.resolve_authorizations = AsyncMock()
        sessions.receive_user_message = AsyncMock()

        result = await sessions.accept_input(
            "session-1",
            "yes",
            delivery_id="input-1",
        )

        sessions.resolve_control.assert_awaited_once_with(
            "session-1", "request-1", approved=True
        )
        sessions.resolve_authorizations.assert_not_awaited()
        sessions.receive_user_message.assert_not_awaited()
        self.assertEqual(result.control_message, "控制操作完成")

async def _explode(_context, _arguments):
    raise RuntimeError("boom")
