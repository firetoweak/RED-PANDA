from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from redpanda.assistant.agents import (
    AGENT_CREATED, COLLEAGUE_JOINED, DirectionDeclared, GroupCreated, fact_arguments,
    identity_for_model, project_agent,
)
from redpanda.assistant.compact.core import window_preamble
from redpanda.automation.routine_tool import SAVE_ROUTINE, SET_ROUTINE_STATE
from redpanda.automation.routines import PAUSED_BY_USER, ROUTINE_DUE
from redpanda.assistant.host.agent_roster import AgentNameTaken, AgentNotFound, AgentRoster
from redpanda.assistant.host.agent_worktrees import GitWorktree
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor, Worker
from redpanda.config import write_json
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.model_settings import ModelConfigurationError, ModelSettings
from redpanda.paths import RedPandaHome
from redpanda.runtime import AgentRuntime, SqliteJournal


class AgentRosterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.home = RedPandaHome(self.root / "home")
        self.store = SessionStore(self.home.runtime_sessions_root)
        self.project = self.root / "project"
        self.project.mkdir()
        self.registry = WorkspaceRegistry.load(self.home.workspaces_path)
        self.workspace = self.registry.register_path(self.project)
        self.host = HostSupervisor(self.store, None, self.home, None, llm=None, workspaces=self.registry)
        # These tests own the roster/fact contract; real Git is covered in process.
        self.git = patch.object(self.host.agent_worktrees, "link", side_effect=lambda project, session_id, path:
                                GitWorktree(path, self.host.agent_worktrees.agent_branch(project.workspace_id, session_id)))
        self.git.start()
        self.addCleanup(self.git.stop)
        # Worker activation is covered in process; here only the requested facts matter.
        self.requests = patch.object(self.host, "request", AsyncMock())
        self.request = self.requests.start()
        self.addCleanup(self.requests.stop)

    async def asyncTearDown(self):
        await self.host.close()
        self.directory.cleanup()

    async def create(self, name="小熊猫", workspace=None):
        return await self.host.create_agent(
            name=name, workspace_id=(workspace or self.workspace).workspace_id,
        )

    async def test_names_are_unique_across_projects_even_for_concurrent_creation(self):
        outcomes = await asyncio.gather(self.create(), self.create(), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, AgentNameTaken) for result in outcomes), 1)
        self.assertEqual(len(self.store.journals()), 1)
        other_project = self.root / "other"
        other_project.mkdir()
        other = self.registry.register_path(other_project)
        with self.assertRaises(AgentNameTaken):
            await self.create(workspace=other)
        first = await self.host.agents.resolve("小熊猫")
        self.assertEqual([item.workspace_id for item in first.projects], [self.workspace.workspace_id])
        with self.assertRaises(AgentNotFound):
            await self.host.agents.resolve("unknown")

    async def test_creation_wakes_the_new_agent_and_tells_existing_colleagues(self):
        first = await self.create("Ada")
        self.request.reset_mock()
        second = await self.create("Lin")
        sent = [(session_id, arguments["fact_type"], arguments["requests_decision"])
                for operation, session_id, arguments in (call.args for call in self.request.await_args_list)]
        # Colleagues hear of the join before the new Agent can act.
        self.assertEqual(sent, [(first.session_id, COLLEAGUE_JOINED, False),
                                (second.session_id, AGENT_CREATED, True)])

    async def test_creation_without_a_usable_default_model_is_refused_before_anything_exists(self):
        # A new Agent decides right after creation; without a model that decision cannot run.
        for default in (None, "deepseek/pro"):
            candidates = [] if default is None else [{"model": default, "compact_threshold_tokens": 1000}]
            write_json(self.home.config_path, {"model": {"default": default, "candidates": candidates}})
            self.host.models = ModelSettings(self.home, self.home.runtime_sessions_root, path=self.home.config_path)
            with self.assertRaises(ModelConfigurationError):
                await self.create()
            self.assertEqual(self.store.journals(), ())
            self.request.assert_not_awaited()

    async def test_retry_finishes_creation_after_model_initialization_failed(self):
        colleague = await self.create("Ada")
        self.request.reset_mock()
        with patch.object(self.host, "models", SimpleNamespace(
                require_default_ready=lambda: None,
                initialize_session=Mock(side_effect=PermissionError("models.json is locked")))):
            with self.assertRaises(PermissionError):
                await self.create()
        self.request.assert_not_awaited()
        rebuilt = AgentRoster(SessionStore(self.store.root))
        entries = await rebuilt.list()
        self.assertEqual(len(entries), 2)
        agent = await rebuilt.resolve("小熊猫")
        self.assertEqual(await self.create(), agent)
        sent = [(sid, args["fact_type"]) for _, sid, args in
                (call.args for call in self.request.await_args_list)]
        self.assertEqual(sent, [(colleague.session_id, COLLEAGUE_JOINED), (agent.session_id, AGENT_CREATED)])
        self.assertFalse((self.store.require(agent.session_id).parent / "agent-creation.json").exists())
        with self.assertRaises(AgentNameTaken):
            await self.create()

    async def test_restart_finishes_a_partial_join_without_notifying_later_colleagues(self):
        first = await self.create("Ada")
        with (patch.object(self.host.agent_messages, "joined", side_effect=OSError("delivery failed")),
              self.assertRaises(OSError)):
            await self.create("Lin")
        partial = await self.host.agents.resolve("Lin")
        later = await self.create("Kai")
        # Simulate a restart with fresh indexes, and a legacy partial creation without a marker.
        (self.store.require(partial.session_id).parent / "agent-creation.json").unlink()
        self.host.agents = AgentRoster(SessionStore(self.store.root))
        self.request.reset_mock()
        with patch.object(self.host, "resume", AsyncMock()):
            await self.host.resume_unfinished_sessions()
        joined = [(sid, args["data"]["name"]) for _, sid, args in
                  (call.args for call in self.request.await_args_list)
                  if args["fact_type"] == COLLEAGUE_JOINED]
        self.assertIn((first.session_id, "Lin"), joined)
        self.assertNotIn((later.session_id, "Lin"), joined)
        self.assertIn(partial.session_id, [call.args[1] for call in self.request.await_args_list
                                         if call.args[2]["fact_type"] == AGENT_CREATED])

    async def test_restart_redelivers_partially_sent_join_notifications_without_duplicates(self):
        first = await self.create("Ada")
        second = await self.create("Kai")

        async def accept(_operation, session_id, arguments):
            runtime = AgentRuntime(SqliteJournal(self.store.require(session_id)), None, {})
            await runtime.receive_domain_fact(session_id, **arguments)

        async def lost_ack(*args):
            await accept(*args)
            raise OSError("notification was committed but its acknowledgement was lost")

        self.request.side_effect = lost_ack
        with self.assertRaises(OSError):
            await self.create("Lin")
        partial = await self.host.agents.resolve("Lin")
        self.host.agents = AgentRoster(SessionStore(self.store.root))
        self.request.side_effect = accept
        with patch.object(self.host, "resume", AsyncMock()):
            await self.host.resume_unfinished_sessions()
        for colleague in (first, second):
            events = await SqliteJournal(self.store.require(colleague.session_id)).snapshot(colleague.session_id)
            from redpanda.runtime import DomainFactCommitted
            notices = [event for event in events if isinstance(event.payload, DomainFactCommitted)
                       and event.payload.fact_type == COLLEAGUE_JOINED and event.payload.data["name"] == "Lin"]
            self.assertEqual(len(notices), 1)
        self.assertFalse((self.store.require(partial.session_id).parent / "agent-creation.json").exists())

    async def test_identity_and_latest_direction_rebuild_from_journals(self):
        agent = await self.create()
        journal = SqliteJournal(self.store.require(agent.session_id))
        runtime = AgentRuntime(journal, None, {})
        for index, direction in enumerate(("研究执行环境", "负责命令执行")):
            await runtime.receive_domain_fact(agent.session_id, **fact_arguments(
                DirectionDeclared(name=agent.name, direction=direction), delivery_id=f"direction-{index}",
            ))
        await self.host.create("ordinary", self.workspace.workspace_id)
        roster = AgentRoster(SessionStore(self.store.root))
        self.assertEqual(await roster.by_session(agent.session_id), agent)
        self.assertIsNone(await roster.by_session("ordinary"))
        entries = await roster.list()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].agent, agent)
        self.assertEqual(entries[0].direction, "负责命令执行")
        linked, = agent.projects
        self.assertEqual(self.registry.get(linked.workspace_id).task_root, self.project)
        self.assertEqual(Path(linked.worktree), agent.directory / "projects" / self.workspace.name)
        self.assertEqual(project_agent(await journal.snapshot(agent.session_id)), agent)
        self.assertFalse((self.home.root / "agents.json").exists())

    async def test_a_saved_routine_fires_into_its_owner_timeline_and_asks_for_a_decision(self):
        agent = await self.create()
        saved = await self.host._route(SAVE_ROUTINE, agent.session_id, {
            "command_id": "cmd-routine", "routine_id": None, "name": "检查 PR",
            "intent": "按 watch-pull-requests 检查。", "schedule": "@every 10m"})
        self.assertEqual(saved["code"], "ROUTINE_SAVED")
        routine, = self.host.list_routines(agent.session_id)
        fire = self.host.routines.store.fire(routine)
        self.request.reset_mock()
        await self.host._deliver_routine(fire)
        operation, session_id, fact = self.request.await_args.args
        self.assertEqual((operation, session_id), ("fact", agent.session_id))
        self.assertEqual((fact["fact_type"], fact["delivery_id"]), (ROUTINE_DUE, fire.fire_id))
        self.assertTrue(fact["requests_decision"])
        self.assertEqual(fact["data"]["intent"], "按 watch-pull-requests 检查。")

        paused = await self.host.set_routine_state_by_user(agent.session_id, routine.routine_id, PAUSED_BY_USER)
        self.assertEqual(paused.state, PAUSED_BY_USER)
        refused = await self.host._route(SET_ROUTINE_STATE, agent.session_id, {
            "command_id": "cmd-resume", "routine_id": routine.routine_id, "state": "active"})
        self.assertEqual(refused["code"], "PAUSED_BY_USER")

    async def test_live_closing_text_reaches_the_person_without_messaging_colleagues(self):
        agent = await self.create()
        await self.create("Lin")
        journal = SqliteJournal(self.store.require(agent.session_id))
        await AgentRuntime(journal, None, {}).receive_user_message(agent.session_id, "你好", delivery_id="hi")
        *_, from_person = await journal.snapshot(agent.session_id)
        self.host.sink = Mock()
        self.request.reset_mock()
        worker = Worker(process=None, peer=None)
        await self.host._route("output", agent.session_id,
                               {"output_id": from_person.event_id, "text": "收尾"}, worker=worker)
        self.assertEqual([call.args for call in self.host.sink.call_args_list], [
            (agent.session_id, from_person.event_id, "收尾"),
        ])
        self.request.assert_not_awaited()

    async def test_new_context_window_restates_identity_before_the_handoff(self):
        agent = await self.create()
        journal = SqliteJournal(self.store.require(agent.session_id))
        runtime = AgentRuntime(journal, None, {})
        for index, fact in enumerate((
            DirectionDeclared(name=agent.name, direction="负责命令执行"),
            DirectionDeclared(name="Lin", direction="负责前端"),
            GroupCreated(group="开发组", members=[agent.name, "Lin"]),
        )):
            await runtime.receive_domain_fact(agent.session_id, **fact_arguments(fact, delivery_id=f"fact-{index}"))
        identity = identity_for_model(await journal.snapshot(agent.session_id))
        self.assertEqual(identity["name"], agent.name)
        self.assertEqual(identity["direction"], "负责命令执行")
        self.assertEqual(identity["groups"], [{"group": "开发组", "members": [agent.name, "Lin"]}])
        window = window_preamble("HANDOFF", 7, None, identity)
        self.assertIn(agent.name, window[0]["content"])
        self.assertIn("HANDOFF", window[1]["content"])
        linked, = agent.projects
        self.assertEqual(identity["projects"], [{"project": linked.name, "directory": linked.worktree}])
        for internal in (agent.session_id, linked.workspace_id, linked.branch):
            self.assertNotIn(internal, window[0]["content"])
