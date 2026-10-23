"""Bots link several projects mid-conversation and unlink them only without losing work, with real Git."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import pytest

from redpanda.assistant.agent_tools import LAND, LINK, UNLINK, agent_git_bindings
from redpanda.assistant.agents import PROJECT_LINKED, PROJECT_UNLINKED
from redpanda.assistant.host.agent_projects import AgentProjects
from redpanda.assistant.host.agent_roster import AgentNameTaken, AgentRoster
from redpanda.runtime import SqliteJournal
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome
from redpanda.runtime.dispatcher import AttemptContext
from tests.assistant.test_agent_worktrees_process import git


pytestmark = pytest.mark.process


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init")
    git(path, "config", "core.autocrlf", "false")
    (path / "note.txt").write_text("initial\n", encoding="utf-8", newline="\n")
    git(path, "add", "note.txt")
    git(path, "commit", "-m", "initial")
    return path


class AgentProjectsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        home = RedPandaHome(self.root / "home")
        self.registry = WorkspaceRegistry.load(home.workspaces_path)
        self.host = HostSupervisor(SessionStore(home.runtime_sessions_root), None, home, None,
                                   llm=None, workspaces=self.registry)
        # Facts the Host writes into the Bot's timeline are what this contract owns.
        self.request = patch.object(self.host, "request", AsyncMock()).start()
        self.addCleanup(patch.stopall)
        self.tools = agent_git_bindings(self.host._route)

    async def asyncTearDown(self):
        await self.host.close()

    async def call(self, tool, agent, arguments):
        context = AttemptContext(agent.session_id, f"{tool}-{len(self.request.await_args_list)}", "attempt", 1)
        return await self.tools[tool].handler(context, arguments)

    def facts(self, kind):
        return [args[2]["data"] for args in (call.args for call in self.request.await_args_list)
                if args[0] == "fact" and args[2]["fact_type"] == kind]

    async def test_creation_reuses_a_directory_for_several_bots(self):
        root = repository(self.root / "shared")
        first = await self.host.create_agent(name="Ada", path=str(root))
        second = await self.host.create_agent(name="Lin", path=str(root))
        project, = self.registry.workspaces
        self.assertEqual(project.name, root.name)
        self.assertEqual(first.projects[0].workspace_id, second.projects[0].workspace_id)
        self.assertNotEqual(first.projects[0].worktree, second.projects[0].worktree)
        self.assertEqual((Path(second.projects[0].worktree) / "note.txt").read_text(), "initial\n")
        extra = repository(self.root / "unused")
        with self.assertRaises(AgentNameTaken):
            await self.host.create_agent(name="Ada", path=str(extra))
        self.assertEqual(len(self.registry.workspaces), 1)

    async def test_restart_completes_link_and_unlink_at_physical_boundaries(self):
        project = self.registry.register_path(repository(self.root / "alpha"))
        agent = await self.host.create_agent(name="Ada")
        projects = self.host.agent_projects
        worktrees = self.host.agent_worktrees
        run = worktrees._git
        branch = worktrees.agent_branch(project.workspace_id, agent.session_id)

        def after_ref(root, *arguments, **options):
            result = run(root, *arguments, **options)
            if arguments[:2] == ("update-ref", f"refs/heads/{branch}"):
                raise EOFError("Host exited after branch creation")
            return result

        with patch.object(worktrees, "_git", side_effect=after_ref):
            with self.assertRaises(EOFError):
                await projects.link(agent.session_id, project, "original-link")
        self.host.agents = AgentRoster(self.host.store)
        projects = self.host.agent_projects = AgentProjects(self.host)
        await projects.recover()
        linked, = (await self.host.agents.by_session(agent.session_id)).projects
        self.assertEqual((Path(linked.worktree) / "note.txt").read_text(), "initial\n")
        await projects.recover()
        events = await SqliteJournal(self.host.store.require(agent.session_id)).snapshot(agent.session_id)
        self.assertEqual(sum(event.delivery is not None and event.delivery.delivery_id == "original-link"
                             for event in events), 1)

        async def lost(_operation, session_id, arguments):
            raise EOFError("Host exited after worktree removal")

        self.request.side_effect = lost
        with self.assertRaises(EOFError):
            await projects.unlink(agent.session_id, linked.name, "original-unlink")
        self.assertFalse(Path(linked.worktree).exists())
        self.host.agents = AgentRoster(self.host.store)
        self.host.agent_projects = AgentProjects(self.host)
        await self.host.agent_projects.recover()
        self.assertEqual((await self.host.agents.by_session(agent.session_id)).projects, ())
        await self.host.agent_projects.recover()
        remove = worktrees.remove

        def lost_cleanup(project, worktree):
            remove(project, worktree)
            raise EOFError("Host exited after rolling back the unaccepted mount")

        with patch.object(worktrees, "remove", side_effect=lost_cleanup):
            with self.assertRaises(BaseExceptionGroup):
                await self.host.agent_projects.link(agent.session_id, project, "unaccepted-link")
        self.host.agent_projects = AgentProjects(self.host)
        await self.host.agent_projects.recover()
        self.assertFalse(Path(linked.worktree).exists())
        self.assertFalse(worktrees._has_branch(project.task_root, linked.branch))
        self.assertEqual((await self.host.agents.by_session(agent.session_id)).projects, ())

    async def test_startup_finishes_only_the_original_prepared_integration_merge(self):
        project = self.registry.register_path(repository(self.root / "alpha"))
        agent = await self.host.create_agent(name="Ada", workspace_id=project.workspace_id)
        linked, = agent.projects
        integration = self.host.agent_worktrees.integration(project.workspace_id)
        for root, name in ((Path(linked.worktree), "bot.txt"), (integration.path, "landed.txt")):
            (root / name).write_text(name)
            git(root, "add", name)
            git(root, "commit", "-m", name)
        owned = self.host.agent_worktrees.git
        run = owned.run

        def before_commit(root, *arguments, **options):
            if arguments[:2] == ("merge", "--no-edit"):
                run(root, "merge", "--no-commit", *arguments[1:], **options)
                raise EOFError("Host exited with clean MERGE_HEAD")
            return run(root, *arguments, **options)

        with patch.object(owned, "run", side_effect=before_commit):
            with self.assertRaises(EOFError):
                await self.host.agent_projects.land(agent.session_id, linked.name)
        self.assertTrue(owned.merging(integration.path))
        self.assertEqual(git(integration.path, "diff", "--name-only", "--diff-filter=U"), "")
        before = git(integration.path, "rev-parse", "HEAD")
        (integration.path / "bot.txt").write_text("changed outside the operation")
        git(integration.path, "add", "bot.txt")
        with self.assertRaisesRegex(ValueError, "original automatic merge"):
            await self.host.agent_projects.recover()
        self.assertEqual(git(integration.path, "rev-parse", "HEAD"), before)
        (integration.path / "bot.txt").write_text("bot.txt")
        git(integration.path, "add", "bot.txt")
        await self.host.agent_projects.recover()
        head = git(integration.path, "rev-parse", "HEAD")
        self.assertFalse(owned.merging(integration.path))
        self.assertTrue((integration.path / "bot.txt").exists())
        await self.host.agent_projects.recover()
        self.assertEqual(git(integration.path, "rev-parse", "HEAD"), head)

    async def test_a_bot_links_projects_into_its_own_directory_mid_conversation(self):
        alpha = repository(self.root / "alpha")
        (self.root / "nested").mkdir()
        other_alpha = repository(self.root / "nested" / "alpha")
        self.registry.register_path(alpha)
        agent = await self.host.create_agent(name="Ada")
        self.assertEqual(self.tools[LINK].requires_authorization({"project": "alpha"}), False)
        self.assertEqual(self.tools[LINK].requires_authorization({"path": str(other_alpha)}), True)

        linked = await self.call(LINK, agent, {"project": "alpha"})
        self.assertEqual((linked["code"], linked["data"]["project"]), ("PROJECT_LINKED", "alpha"))
        worktree = Path(linked["data"]["directory"])
        self.assertEqual(worktree, agent.directory / "projects" / "alpha")
        self.assertEqual((worktree / "note.txt").read_text(encoding="utf-8"), "initial\n")
        self.assertEqual((await self.call(LINK, agent, {"project": "alpha"}))["code"], "ALREADY_LINKED")
        # A second project with the same name is registered in place and gets its own directory.
        second = await self.call(LINK, agent, {"path": str(other_alpha)})
        self.assertEqual((second["code"], second["data"]["project"]), ("PROJECT_LINKED", "alpha-2"))
        self.assertEqual([record.task_root for record in self.registry.workspaces], [alpha, other_alpha])
        self.assertEqual((await self.call(LINK, agent, {"project": "missing"}))["code"], "PROJECT_NOT_FOUND")
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual((await self.call(LINK, agent, {"path": str(plain)}))["code"], "PROJECT_UNUSABLE")
        self.assertEqual(len(self.registry.workspaces), 2)

        current = await self.host.agents.by_session(agent.session_id)
        self.assertEqual([item.name for item in current.projects], ["alpha", "alpha-2"])
        self.assertEqual([fact["name"] for fact in self.facts(PROJECT_LINKED)], ["alpha", "alpha-2"])
        self.assertEqual(git(alpha, "status", "--porcelain"), "")

    async def test_unlinking_never_loses_uncommitted_or_unlanded_work(self):
        self.registry.register_path(repository(self.root / "alpha"))
        agent = await self.host.create_agent(name="Ada", workspace_id=self.registry.workspaces[0].workspace_id)
        worktree = Path(agent.projects[0].worktree)
        (worktree / "draft.txt").write_text("draft\n", encoding="utf-8")
        refused = await self.call(UNLINK, agent, {"project": "alpha"})
        self.assertEqual((refused["code"], refused["data"]["uncommitted"]), ("UNLINK_BLOCKED", ["draft.txt"]))
        git(worktree, "add", "draft.txt")
        git(worktree, "commit", "-m", "draft")
        refused = await self.call(UNLINK, agent, {"project": "alpha"})
        self.assertEqual((refused["code"], refused["data"]["unlanded_commits"]), ("UNLINK_BLOCKED", 1))
        self.assertTrue((worktree / "draft.txt").exists())

        self.assertEqual((await self.call(LAND, agent, {"project": "alpha"}))["code"], "LAND_COMPLETED")
        self.assertEqual((await self.call(UNLINK, agent, {"project": "alpha"}))["code"], "PROJECT_UNLINKED")
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.registry.workspaces[0].task_root, "branch", "--list", agent.projects[0].branch), "")
        self.assertEqual((await self.host.agents.by_session(agent.session_id)).projects, ())
        self.assertEqual([fact["name"] for fact in self.facts(PROJECT_UNLINKED)], ["alpha"])
        # Linking again starts a fresh branch from what was landed.
        again = await self.call(LINK, agent, {"project": "alpha"})
        self.assertTrue((Path(again["data"]["directory"]) / "draft.txt").exists())
