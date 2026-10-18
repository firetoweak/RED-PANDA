from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from redpanda.assistant.agents import (
    AGENT_CREATED, COLLEAGUE_JOINED, DirectionDeclared, GroupCreated, fact_arguments,
    identity_for_model, project_agent,
)
from redpanda.assistant.compact.core import window_preamble
from redpanda.assistant.host.agent_roster import AgentNameTaken, AgentNotFound, AgentRoster
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
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
        self.git = patch.object(self.host.agent_worktrees, "create", side_effect=lambda project, session_id:
                                self.host.agent_worktrees.agent(project.workspace_id, session_id))
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

    async def test_names_are_unique_per_project_even_for_concurrent_creation(self):
        outcomes = await asyncio.gather(self.create(), self.create(), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, AgentNameTaken) for result in outcomes), 1)
        self.assertEqual(len(self.store.journals()), 1)
        other_project = self.root / "other"
        other_project.mkdir()
        other = self.registry.register_path(other_project)
        second = await self.create(workspace=other)
        first = await self.host.agents.resolve(self.workspace.workspace_id, "小熊猫")
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(await self.host.agents.resolve(other.workspace_id, "小熊猫"), second)
        with self.assertRaises(AgentNotFound):
            await self.host.agents.resolve(other.workspace_id, "unknown")

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
        self.assertEqual(self.registry.get(agent.workspace_id).task_root, self.project)
        self.assertEqual(agent.worktree, self.host.agent_worktrees.agent(agent.workspace_id, agent.session_id).path)
        self.assertEqual(project_agent(await journal.snapshot(agent.session_id)), agent)
        self.assertFalse((self.home.root / "agents.json").exists())

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
        for internal in (agent.session_id, agent.workspace_id, agent.identity.branch):
            self.assertNotIn(internal, window[0]["content"])
