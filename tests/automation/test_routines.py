from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from datetime import timedelta
from unittest.mock import patch

from redpanda.automation.routines import (
    ACTIVE, PAUSED, PAUSED_BY_USER, RoutineStore,
)
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.runtime import AgentRuntime, SqliteJournal


class RoutineStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "routines.sqlite"
        self.store = RoutineStore(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def save(self, command_id="cmd-1", **changes):
        arguments = {"routine_id": None, "name": "检查 PR", "intent": "按 watch-pull-requests 检查。",
                     "schedule": "*/10 9-18 * * 1-5", **changes}
        return self.store.save(command_id, "agent", **arguments)

    def test_a_request_is_applied_once_and_a_replay_returns_the_first_result(self):
        first = self.save()
        self.assertEqual(first["code"], "ROUTINE_SAVED")
        self.assertEqual(self.save(name="换了名字"), first)
        self.assertEqual(len(self.store.list("agent")), 1)
        refused = self.save("cmd-2", schedule="@every 4m")
        self.assertEqual(self.save("cmd-2"), refused)

    def test_schedules_run_at_most_every_five_minutes(self):
        for schedule, code in (("@every 4m", "SCHEDULE_TOO_FREQUENT"), ("* * * * *", "SCHEDULE_TOO_FREQUENT"),
                               ("every day", "INVALID_SCHEDULE"), ("@every 5m", "ROUTINE_SAVED"),
                               ("0 9 * * 1-5", "ROUTINE_SAVED")):
            with self.subTest(schedule=schedule):
                self.assertEqual(self.save(f"cmd-{schedule}", schedule=schedule)["code"], code)

    def test_a_fire_advances_the_schedule_and_a_changed_routine_is_not_fired(self):
        routine_id = self.save()["data"]["routine_id"]
        stale, = self.store.list("agent")
        self.save("cmd-2", routine_id=routine_id, intent="改过的意图。")
        self.assertIsNone(self.store.fire(stale))

        current, = self.store.list("agent")
        fire = self.store.fire(current)
        self.assertEqual(fire.intent, "改过的意图。")
        advanced, = self.store.list("agent")
        self.assertGreater(advanced.next_due, current.next_due)
        self.assertIsNone(self.store.fire(current))
        # A fire recorded but not yet delivered survives a restart with the same identity.
        self.assertEqual(RoutineStore(self.path).undelivered().fire_id, fire.fire_id)
        self.store.delivered(fire.fire_id)
        self.assertIsNone(self.store.undelivered())

    def test_a_person_pause_holds_until_a_person_resumes(self):
        routine_id = self.save()["data"]["routine_id"]
        self.assertEqual(self.store.set_state("cmd-2", "agent", routine_id, PAUSED)["data"]["state"], PAUSED)
        paused = self.store.set_state_by_user(routine_id, PAUSED_BY_USER)
        self.assertIsNone(paused.next_due)
        self.assertIsNone(self.store.next_due())
        self.assertEqual(self.store.set_state("cmd-3", "agent", routine_id, ACTIVE)["code"], "PAUSED_BY_USER")
        resumed = self.store.set_state_by_user(routine_id, ACTIVE)
        self.assertEqual(resumed.state, ACTIVE)
        self.assertIsNotNone(resumed.next_due)
        self.assertEqual(self.store.set_state("cmd-4", "agent", routine_id, "deleted")["code"], "ROUTINE_DELETED")
        self.assertEqual(self.store.list("agent"), ())

    def test_a_fired_fact_keeps_its_content_after_edit_and_restart(self):
        routine_id = self.save()["data"]["routine_id"]
        original = self.store.fire(self.store.list("agent")[0])
        runtime = AgentRuntime(SqliteJournal(self.path.parent / "journal.sqlite"), None, {})

        async def receive(operation, session_id, arguments):
            self.assertEqual(operation, "fact")
            await runtime.receive_domain_fact(session_id, **arguments)

        async def redeliver():
            host = SimpleNamespace(request=receive)
            await HostSupervisor._deliver_routine(host, original)
            # The fact arrived, but the clock lost its acknowledgement before the edit.
            self.save("edit", routine_id=routine_id, name="新的名字", intent="新的意图。")
            restored = RoutineStore(self.path)
            recovered = restored.undelivered()
            self.assertEqual(recovered, original)
            await HostSupervisor._deliver_routine(host, recovered)
            self.assertEqual(len(await runtime.snapshot("agent")), 1)
            restored.delivered(original.fire_id)
            history = restored.fires("agent", routine_id)[0]
            self.assertEqual((history.name, history.intent), (original.name, original.intent))
            following = restored.fire(restored.list("agent")[0])
            self.assertEqual((following.name, following.intent), ("新的名字", "新的意图。"))

        asyncio.run(redeliver())

    def test_schema_without_frozen_fire_content_is_rejected(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 2")
        with self.assertRaisesRegex(ValueError, "unsupported routine schema version: 2"):
            RoutineStore(self.path)

    def test_every_keeps_its_phase_when_waking_late_and_skips_missed_slots(self):
        self.save(schedule="@every 5m")
        routine, = self.store.list("agent")
        anchor = routine.next_due
        for lateness, intervals in ((timedelta(milliseconds=7), 1),
                                    (timedelta(minutes=12, milliseconds=13), 3)):
            with patch("redpanda.automation.routines.datetime") as clock:
                clock.now.return_value = routine.next_due + lateness
                # Stored values still decode through the real datetime type.
                from datetime import datetime
                clock.fromisoformat = datetime.fromisoformat
                self.store.fire(routine)
            advanced, = self.store.list("agent")
            self.assertEqual(advanced.next_due, routine.next_due + intervals * timedelta(minutes=5))
            self.assertEqual((advanced.next_due - anchor) % timedelta(minutes=5), timedelta())
            routine = advanced
