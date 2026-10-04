from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from redpanda.assistant.artifacts import MemoryArtifactGateway
from redpanda.assistant.context.projection import ModelContextSettings
from redpanda.assistant.management import ManagementSurface
from redpanda.assistant.sessions import AssistantSessions
from redpanda.assistant.toolsets import ToolSurface
from redpanda.runtime import (
    AgentRuntime,
    CommandPhase,
    DispatchAttemptStarted,
    InvokeTool,
    ModelDecision,
    SqliteJournal,
    ToolBinding,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.session_scheduler import RecordingScheduler, SettlingScheduler


class EmptyCatalog:
    def rehydrate(self, _session_id, _events):
        return None

    async def sync(self, _runtime, _session_id):
        return None


class DurableRuntimeSliceTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_session_identity_is_idempotent_and_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            journal = SqliteJournal(path)
            self.assertTrue(await journal.create_session("session"))
            self.assertFalse(await journal.create_session("session"))
            self.assertTrue(await SqliteJournal(path).session_exists("session"))

    async def test_delivery_deduplicates_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            first = SqliteJournal(path)
            await first.create_session("session")
            runtime = AgentRuntime(
                first,
                ScriptedDecisionMaker((lambda _frame: ModelDecision(content="one"),)),
                {},
                SequentialIds(),
            )
            event = await runtime.receive_user_message(
                "session",
                "hello",
                delivery_id="delivery-1",
            )

            restarted = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(()),
                {},
                SequentialIds(),
            )
            duplicate = await restarted.receive_user_message(
                "session",
                "hello",
                delivery_id="delivery-1",
            )

            self.assertEqual(duplicate.event_id, event.event_id)
            self.assertEqual(len(await restarted.snapshot("session")), 1)

    async def test_domain_fact_delivery_deduplicates_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            first = SqliteJournal(path)
            await first.create_session("session")
            runtime = AgentRuntime(
                first,
                ScriptedDecisionMaker(()),
                {},
                SequentialIds(),
            )
            event = await runtime.receive_domain_fact(
                "session",
                "automation.fired",
                {"scheduled_for": "2026-09-02T09:00:00Z"},
                delivery_id="policy-1:2026-09-02T09:00:00Z",
                source="automation",
                requests_decision=True,
            )

            restarted = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(()),
                {},
                SequentialIds(),
            )
            duplicate = await restarted.receive_domain_fact(
                "session",
                "automation.fired",
                {"scheduled_for": "2026-09-02T09:00:00Z"},
                delivery_id="policy-1:2026-09-02T09:00:00Z",
                source="automation",
                requests_decision=True,
            )

            self.assertEqual(duplicate.event_id, event.event_id)
            self.assertEqual(len(await restarted.snapshot("session")), 1)

    async def test_step_and_command_outcome_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            model = ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(InvokeTool("echo"),),
                    ),
                    lambda _frame: ModelDecision(content="done"),
                )
            )
            runtime = AgentRuntime(
                SqliteJournal(path),
                model,
                {"echo": ToolBinding(_echo)},
                SequentialIds(),
            )
            scheduler = SettlingScheduler(runtime, "session")
            await runtime.create_session("session")
            try:
                await runtime.receive_user_message(
                    "session",
                    "go",
                    delivery_id="delivery-1",
                )
                await scheduler.wake("session")
                await scheduler.join()
            finally:
                await scheduler.close()

            restarted = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(()),
                {"echo": ToolBinding(_echo)},
                SequentialIds(),
            )
            state = await restarted.state("session")
            self.assertEqual(len(state.steps), 2)
            self.assertTrue(
                all(
                    command.phase is CommandPhase.TERMINAL for command in state.commands
                )
            )

    async def test_failed_unrecorded_attempt_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            runtime = AgentRuntime(
                SqliteJournal(path),
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
            scheduler = SettlingScheduler(runtime, "session")
            await runtime.create_session("session")
            await runtime.receive_user_message(
                "session",
                "go",
                delivery_id="delivery-1",
            )
            await scheduler.wake("session")
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await scheduler.join()
            await scheduler.close()

            state = await AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(()),
                {"explode": ToolBinding(_explode)},
                SequentialIds(),
            ).state("session")
            self.assertEqual(
                state.commands[0].phase,
                CommandPhase.UNKNOWN,
            )

    async def test_resume_and_wake_do_not_retry_unknown_attempt(self):
        explode_calls: list[int] = []

        async def explode(_context, _arguments):
            explode_calls.append(1)
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            runtime = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(
                    (
                        lambda _frame: ModelDecision(
                            command_requests=(InvokeTool("explode"),),
                        ),
                    )
                ),
                {"explode": ToolBinding(explode)},
                SequentialIds(),
            )
            scheduler = SettlingScheduler(runtime, "session")
            await runtime.create_session("session")
            await runtime.receive_user_message(
                "session",
                "go",
                delivery_id="delivery-1",
            )
            await scheduler.wake("session")
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await scheduler.join()
            await scheduler.close()
            self.assertEqual(explode_calls, [1])

            restored = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(()),
                {"explode": ToolBinding(explode)},
                SequentialIds(),
            )
            restored_scheduler = RecordingScheduler(restored, "session")
            surface = ToolSurface()
            surface.attach(restored)
            sessions = AssistantSessions(
                restored,
                surface,
                restored_scheduler,
                control=restored_scheduler._control,
                management=ManagementSurface(
                    (),
                    MemoryArtifactGateway(),
                    ModelContextSettings(),
                ),
                catalog=EmptyCatalog(),
            )
            try:
                view = await sessions.resume("session")
                state = await restored.state("session")
                self.assertEqual(state.commands[0].phase, CommandPhase.UNKNOWN)
                self.assertEqual(len(state.commands[0].attempts), 1)
                self.assertEqual(state.commands[0].attempts[0].attempt_number, 1)
                self.assertFalse(view.should_wake)
                self.assertEqual(restored_scheduler.woken, [])

                await restored_scheduler.wake("session")
                await restored_scheduler.join()
                after_wake = await restored.state("session")
                started = [
                    event.payload
                    for event in await restored.snapshot("session")
                    if isinstance(event.payload, DispatchAttemptStarted)
                ]
                self.assertEqual(len(started), 1)
                self.assertEqual(started[0].attempt_number, 1)
                self.assertEqual(after_wake.commands[0].phase, CommandPhase.UNKNOWN)
                self.assertEqual(len(after_wake.commands[0].attempts), 1)
                self.assertEqual(explode_calls, [1])
            finally:
                await restored_scheduler.close()


async def _echo(_context, _arguments):
    return "ok"


async def _explode(_context, _arguments):
    raise RuntimeError("boom")
