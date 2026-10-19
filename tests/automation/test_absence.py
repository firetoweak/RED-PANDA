from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from redpanda.assistant.agents import ROUTINES_UNWATCHED
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.automation.absence import ASK_GRACE, UNREAD_REPORTS, next_absence_action
from redpanda.automation.routines import ACTIVE, PAUSED_BY_USER, PAUSED_UNWATCHED, RoutineStore
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome
from redpanda.runtime import DomainFactCommitted, SqliteJournal


NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


class AbsenceDecisionTest(unittest.TestCase):
    def test_away_counts_ask_once_then_pause_after_the_grace(self):
        away = NOW - timedelta(hours=13)
        self.assertEqual(next_absence_action(
            present=True, now=NOW, last_seen=away, watch=None, reports=UNREAD_REPORTS, triggers=UNREAD_REPORTS,
        ), None)
        self.assertEqual(next_absence_action(
            present=False, now=NOW, last_seen=NOW, watch=None, reports=UNREAD_REPORTS, triggers=UNREAD_REPORTS,
        ), None)
        self.assertEqual(next_absence_action(
            present=False, now=NOW, last_seen=away, watch=None, reports=UNREAD_REPORTS - 1, triggers=UNREAD_REPORTS,
        ), None)
        self.assertEqual(next_absence_action(
            present=False, now=NOW, last_seen=away, watch=None, reports=UNREAD_REPORTS, triggers=UNREAD_REPORTS,
        ), "ask")

    def test_pause_and_resume_touch_only_routines_that_were_still_running(self):
        with TemporaryDirectory() as directory:
            store = RoutineStore(Path(directory) / "routines.sqlite")
            running = store.save("run", "ada", routine_id=None, name="检查", intent="看", schedule="@every 10m")
            own = store.save("own", "ada", routine_id=None, name="自停", intent="看", schedule="@every 10m")
            human = store.save("human", "ada", routine_id=None, name="人停", intent="看", schedule="@every 10m")
            store.set_state("own-pause", "ada", own["data"]["routine_id"], "paused")
            store.set_state_by_user(human["data"]["routine_id"], PAUSED_BY_USER)
            asked = store.ask_absence("ada", NOW)
            again = store.ask_absence("ada", NOW + timedelta(hours=1))
            self.assertEqual(again.asked_at, asked.asked_at)
            paused = store.pause_for_absence("ada")
            running_id = running["data"]["routine_id"]
            self.assertEqual(paused.paused_ids, (running_id,))
            self.assertEqual(store.pause_for_absence("ada").paused_ids, paused.paused_ids)
            by_id = {item.routine_id: item.state for item in store.list("ada")}
            self.assertEqual(by_id[running_id], PAUSED_UNWATCHED)
            self.assertEqual(store.set_state("resume", "ada", running_id, ACTIVE)["code"], "PAUSED_UNWATCHED")
            restored = store.resume_absence("ada", NOW)
            self.assertEqual(restored, (running_id,))
            self.assertIsNone(store.watch("ada"))
            by_id = {item.routine_id: item.state for item in store.list("ada")}
            self.assertEqual(by_id[running_id], ACTIVE)
            self.assertEqual(by_id[human["data"]["routine_id"]], PAUSED_BY_USER)


class AbsenceReviewTest(unittest.IsolatedAsyncioTestCase):
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
            side_effect=lambda workspace, session_id: self.host.agent_worktrees.agent(workspace.workspace_id, session_id),
        )
        self.git.start()
        self.requests = patch.object(self.host, "request", AsyncMock())
        self.requests.start()

    async def asyncTearDown(self):
        self.requests.stop()
        self.git.stop()
        await self.host.close()
        self.directory.cleanup()

    async def test_review_asks_then_pauses_and_tells_the_agent_once(self):
        agent = await self.host.create_agent(name="Ada", workspace_id=self.workspace.workspace_id)
        saved = self.host.routines.store.save(
            "routine", agent.session_id, routine_id=None, name="检查", intent="看", schedule="@every 10m",
        )
        routine_id = saved["data"]["routine_id"]
        away = NOW - timedelta(hours=13)
        self.host.routines.store.note_disconnected(away)
        with patch("redpanda.assistant.host.supervisor.counts_since", return_value=(UNREAD_REPORTS, UNREAD_REPORTS)):
            await self.host.review_absence(NOW)
            self.assertEqual(self.host.routines.store.watch(agent.session_id).phase, "asked")
            await self.host.review_absence(NOW + ASK_GRACE)
        watch = self.host.routines.store.watch(agent.session_id)
        self.assertEqual(watch.phase, "paused")
        self.assertTrue(watch.fact_recorded)
        self.assertEqual(self.host.routines.store.list(agent.session_id)[0].routine_id, routine_id)
        self.assertEqual(self.host.routines.store.list(agent.session_id)[0].state, PAUSED_UNWATCHED)
        events = await SqliteJournal(self.store.require(agent.session_id)).snapshot(agent.session_id)
        notices = [event for event in events if isinstance(event.payload, DomainFactCommitted)
                   and event.payload.fact_type == ROUTINES_UNWATCHED]
        self.assertEqual(len(notices), 1)
        self.assertFalse(notices[0].payload.requests_decision)
        await self.host.answer_absence(agent.session_id, "continue")
        self.assertEqual(self.host.routines.store.list(agent.session_id)[0].state, ACTIVE)
        self.assertIsNone(self.host.routines.store.watch(agent.session_id))
