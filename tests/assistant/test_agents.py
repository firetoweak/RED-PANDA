from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from redpanda.assistant.agents import DirectionDeclared, fact_arguments, project_agent
from redpanda.assistant.conversations import AssistantQueries, FileChangesUnavailable
from redpanda.assistant.host.agent_roster import AgentNameTaken, AgentNotFound, AgentRoster
from redpanda.assistant.host.session_store import SessionForkUnavailableError, SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
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

    async def test_existing_history_branch_entry_cannot_clone_an_agent_identity(self):
        agent = await self.create()
        journal = SqliteJournal(self.store.require(agent.session_id))
        creation = (await journal.snapshot(agent.session_id))[-1]
        with self.assertRaises(SessionForkUnavailableError):
            await self.store.fork_after_event(agent.session_id, creation.event_id, "clone")
        self.assertFalse(self.store.path("clone").exists())
        self.assertEqual(len(await self.host.agents.list()), 1)

    async def test_existing_change_query_cannot_reconnect_an_agent_to_agentfs(self):
        agent = await self.create()
        with patch("redpanda.assistant.conversations.workspace_files", side_effect=AssertionError("AgentFS")):
            with self.assertRaisesRegex(FileChangesUnavailable, "Git"):
                await AssistantQueries(self.store, self.host).file_changes(agent.session_id)
