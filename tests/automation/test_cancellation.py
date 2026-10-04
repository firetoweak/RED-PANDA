from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.automation.once import OneShotClock, OneShotSchedules
from redpanda.automation.recovery import recover_schedule_attempts
from redpanda.automation.tool import CANCEL_SCHEDULE, cancel_schedule_binding
from redpanda.runtime import (
    AgentRuntime, CommandOutcomeReceived, CommandPhase, InvokeTool,
    ModelDecision, SqliteJournal,
)
from redpanda.runtime.dispatcher import AttemptContext
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.session_scheduler import SettlingScheduler


class CancellationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = OneShotSchedules(self.root / "automation.sqlite")

    def test_cancel_survives_restart_and_registration_recovery(self):
        self.store.register("check", "session", 30, "observe")
        self.assertEqual(self.store.cancel("cancel", "session", "check"), "CANCELLED")

        restored = OneShotSchedules(self.store.path)
        restored.register("check", "session", 30, "observe")
        self.assertIsNone(restored.next_pending())
        self.assertIsNone(restored.firing_time("check"))
        self.assertEqual(restored.cancel("cancel", "session", "check"), "CANCELLED")
        self.assertEqual(restored.cancel("cancel-again", "session", "check"), "CANCELLED")

    def test_trigger_wins_even_before_delivery_finishes(self):
        self.store.register("check", "session", 30, "observe")
        fired = self.store.firing_time("check")
        self.assertEqual(self.store.cancel("cancel", "session", "check"), "ALREADY_FIRED")
        self.assertEqual(self.store.next_pending().schedule_id, "check")
        self.assertEqual(self.store.firing_time("check"), fired)
        self.store.delivered("check")
        self.assertEqual(self.store.cancel("cancel-again", "session", "check"), "ALREADY_FIRED")

    def test_cancel_cannot_affect_another_session(self):
        self.store.register("check", "owner", 30, "observe")
        self.assertEqual(self.store.cancel("cancel", "other", "check"), "NOT_FOUND")
        self.assertEqual(self.store.next_pending("owner").schedule_id, "check")
        self.assertIsNotNone(self.store.firing_time("check"))

    def test_recovered_cancel_keeps_original_result(self):
        self.assertEqual(self.store.cancel("cancel", "session", "check"), "NOT_FOUND")
        self.store.register("check", "session", 30, "observe")
        self.assertEqual(self.store.cancel("cancel", "session", "check"), "NOT_FOUND")
        self.assertIsNotNone(self.store.next_pending())
        with self.assertRaisesRegex(ValueError, "another request"):
            self.store.cancel("cancel", "session", "different")

    async def test_cancel_wakes_clock_and_updates_countdown_projection(self):
        host = object.__new__(HostSupervisor)
        host.automation = OneShotClock(self.store)
        host.schedule_changed_sink = AsyncMock()
        host.automation.register("check", "session", 300, "observe")
        host.automation.register("next", "session", 600, "observe again")
        delivered = asyncio.Queue()

        async def deliver(schedule, _fired_at):
            delivered.put_nowait(schedule.schedule_id)

        task = asyncio.create_task(host.automation.run(deliver))
        try:
            await asyncio.sleep(0)
            binding = cancel_schedule_binding(host._route)
            result = await binding.handler(
                AttemptContext("session", "cancel", "attempt", 1),
                {"schedule_id": "check"},
            )
            self.assertEqual(result["code"], "CANCELLED")
            self.assertEqual(host.next_scheduled_check("session").schedule_id, "next")
            await binding.handler(
                AttemptContext("session", "cancel-next", "attempt-next", 1),
                {"schedule_id": "next"},
            )
            self.assertIsNone(host.next_scheduled_check("session"))
            self.assertEqual(host.schedule_changed_sink.await_count, 2)
            host.automation.register(
                "sentinel", "session", 1, "end test",
                started_at=datetime.now(timezone.utc) - timedelta(seconds=2),
            )
            self.assertEqual(await asyncio.wait_for(delivered.get(), timeout=2), "sentinel")
            self.assertTrue(delivered.empty())
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_invalid_model_input_does_not_reach_host(self):
        transport = AsyncMock()
        binding = cancel_schedule_binding(transport)
        for arguments in ({}, {"schedule_id": 1}, {"schedule_id": " "}, {"schedule_id": "x", "extra": True}):
            with self.subTest(arguments=arguments):
                result = await binding.handler(
                    AttemptContext("session", "cancel", "attempt", 1), arguments,
                )
                self.assertEqual(result["code"], "INVALID_ARGUMENT")
        transport.assert_not_awaited()

    async def test_cancel_outcome_recovers_after_worker_failure(self):
        self.store.register("check", "session", 30, "observe")

        async def transport(_operation, session_id, arguments):
            return self.store.cancel(arguments["command_id"], session_id, arguments["schedule_id"])

        async def interrupted(*args):
            await transport(*args)
            raise RuntimeError("worker died after cancellation")

        journal = SqliteJournal(self.root / "journal.sqlite")
        runtime = AgentRuntime(
            journal,
            ScriptedDecisionMaker((lambda _frame: ModelDecision(command_requests=(
                InvokeTool(CANCEL_SCHEDULE, (("schedule_id", "check"),)),
            )),)),
            {CANCEL_SCHEDULE: cancel_schedule_binding(interrupted)},
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")
        await runtime.receive_user_message("session", "cancel", delivery_id="input")
        try:
            await scheduler.wake("session")
            with self.assertRaisesRegex(RuntimeError, "worker died"):
                await scheduler.join()
        finally:
            await scheduler.close()
        self.assertEqual((await runtime.state("session")).commands[0].phase, CommandPhase.UNKNOWN)
        self.store = OneShotSchedules(self.store.path)
        await recover_schedule_attempts(journal, "session", transport)
        await recover_schedule_attempts(journal, "session", transport)
        outcomes = [e.payload for e in await journal.snapshot("session") if isinstance(e.payload, CommandOutcomeReceived)]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].outcome.value["code"], "CANCELLED")
        self.assertIsNone(self.store.next_pending())
