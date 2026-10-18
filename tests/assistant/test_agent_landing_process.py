"""Agents land into the project integration branch and sync from it with real Git."""
from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import pytest

from redpanda.assistant.agent_tools import LAND, SYNC, agent_git_bindings
from redpanda.assistant.conversations import AssistantQueries
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome
from redpanda.runtime.dispatcher import AttemptContext
from tests.assistant.test_agent_worktrees_process import git


pytestmark = pytest.mark.process


class AgentLandingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name).resolve()
        self.project = root / "project"
        self.project.mkdir()
        git(self.project, "init")
        git(self.project, "config", "core.autocrlf", "false")
        (self.project / "note.txt").write_text("initial\n", encoding="utf-8", newline="\n")
        git(self.project, "add", "note.txt")
        git(self.project, "commit", "-m", "initial")
        home = RedPandaHome(root / "home")
        registry = WorkspaceRegistry.load(home.workspaces_path)
        self.workspace = registry.register_path(self.project)
        self.host = HostSupervisor(SessionStore(home.runtime_sessions_root), None, home, None,
                                   llm=None, workspaces=registry)
        # These tests own landing; waking the new Agent is covered with a real Worker elsewhere.
        activation = patch.object(self.host, "request", AsyncMock())
        activation.start()
        self.addCleanup(activation.stop)
        # The Worker transport ends at the Host route; drive the model tools through it.
        self.tools = agent_git_bindings(self.host._route)
        self.integration = self.host.agent_worktrees.integration(self.workspace.workspace_id).path

    async def asyncTearDown(self):
        await self.host.close()

    async def agent(self, name):
        return await self.host.create_agent(name=name, workspace_id=self.workspace.workspace_id)

    def change(self, agent, path, text):
        """The Agent commits its own work."""
        (agent.worktree / path).write_text(text, encoding="utf-8", newline="\n")
        git(agent.worktree, "add", path)
        git(agent.worktree, "commit", "-m", f"change {path}")
        return git(agent.worktree, "rev-parse", "HEAD")

    async def call(self, tool, agent, arguments=None):
        context = AttemptContext(agent.session_id, f"{tool}-call", f"{tool}-attempt", 1)
        return await self.tools[tool].handler(context, arguments or {})

    async def test_conflicts_never_change_integration_and_are_resolved_on_the_agent_branch(self):
        human = git(self.project, "rev-parse", "HEAD")
        first, second = await self.agent("甲"), await self.agent("乙")
        self.assertEqual((await self.call(LAND, first))["code"], "LAND_UNCHANGED")
        self.assertEqual((await self.call(LAND, first, {"force": True}))["code"], "INVALID_ARGUMENT")
        landed = self.change(first, "note.txt", "first\n")
        result = await self.call(LAND, first)
        self.assertEqual((result["code"], result["data"]), ("LAND_COMPLETED", {"commit": landed, "fast_forward": True}))

        self.change(second, "note.txt", "second\n")
        result = await self.call(LAND, second)
        self.assertEqual((result["code"], result["data"]), ("LAND_CONFLICT", {"files": ["note.txt"]}))
        self.assertEqual(git(self.integration, "rev-parse", "HEAD"), landed)
        self.assertEqual(git(self.integration, "status", "--porcelain"), "")

        result = await self.call(SYNC, second)
        self.assertEqual((result["code"], result["data"]), ("SYNC_CONFLICT", {"files": ["note.txt"]}))
        self.assertIn("<<<<<<<", (second.worktree / "note.txt").read_text(encoding="utf-8"))
        self.assertEqual((await self.call(SYNC, second))["code"], "SYNC_MERGE_IN_PROGRESS")
        (second.worktree / "note.txt").write_text("both\n", encoding="utf-8")
        git(second.worktree, "add", "note.txt")
        git(second.worktree, "commit", "--no-edit")

        later = self.change(first, "first.txt", "more\n")
        self.assertEqual((await self.call(LAND, first))["data"], {"commit": later, "fast_forward": True})
        result = await self.call(LAND, second)
        self.assertEqual((result["code"], result["data"]["fast_forward"]), ("LAND_COMPLETED", False))
        merge = git(self.integration, "log", "-1", "--format=%H %an %P").split()
        self.assertEqual(merge[:2], [result["data"]["commit"], "乙"])
        self.assertEqual(merge[2:], [later, git(second.worktree, "rev-parse", "HEAD")])
        self.assertEqual((self.integration / "note.txt").read_text(encoding="utf-8"), "both\n")

        result = await self.call(SYNC, first)
        self.assertEqual((result["code"], result["data"]["fast_forward"]), ("SYNC_COMPLETED", True))
        self.assertEqual((await self.call(SYNC, first))["code"], "SYNC_UNCHANGED")
        self.assertEqual(git(self.project, "rev-parse", "HEAD"), human)
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    async def test_concurrent_landings_of_one_project_are_serialized(self):
        agents = [await self.agent(f"同事{index}") for index in range(4)]
        for index, agent in enumerate(agents):
            self.change(agent, f"part{index}.txt", f"{index}\n")
        results = await asyncio.gather(*(self.call(LAND, agent) for agent in agents))
        self.assertEqual({result["code"] for result in results}, {"LAND_COMPLETED"})
        self.assertEqual(sorted(path.name for path in self.integration.glob("part*.txt")),
                         [f"part{index}.txt" for index in range(4)])
        self.assertEqual(git(self.integration, "status", "--porcelain"), "")

    async def test_sync_does_not_overwrite_uncommitted_files(self):
        first, second = await self.agent("甲"), await self.agent("乙")
        self.change(first, "shared.txt", "landed\n")
        await self.call(LAND, first)
        (second.worktree / "shared.txt").write_text("local\n", encoding="utf-8")
        head = git(second.worktree, "rev-parse", "HEAD")
        result = await self.call(SYNC, second)
        self.assertEqual(result["code"], "SYNC_BLOCKED")
        self.assertIn("shared.txt", result["data"]["git"])
        self.assertEqual((second.worktree / "shared.txt").read_text(encoding="utf-8"), "local\n")
        self.assertEqual(git(second.worktree, "rev-parse", "HEAD"), head)

    async def test_landing_does_not_overwrite_files_left_in_integration(self):
        agent = await self.agent("甲")
        self.change(agent, "part.txt", "landed\n")
        (self.integration / "part.txt").write_text("left\n", encoding="utf-8")
        head = git(self.integration, "rev-parse", "HEAD")
        result = await self.call(LAND, agent)
        self.assertEqual((result["code"], result["data"]), ("LAND_BLOCKED", {"files": ["part.txt"]}))
        self.assertEqual((self.integration / "part.txt").read_text(encoding="utf-8"), "left\n")
        self.assertEqual(git(self.integration, "rev-parse", "HEAD"), head)

    async def test_staged_changes_in_integration_block_a_real_merge_and_are_listed(self):
        first, second = await self.agent("甲"), await self.agent("乙")
        self.change(first, "first.txt", "first\n")
        await self.call(LAND, first)
        self.change(second, "second.txt", "second\n")
        (self.integration / "note.txt").write_text("staged\n", encoding="utf-8")
        git(self.integration, "add", "note.txt")
        result = await self.call(LAND, second)
        self.assertEqual((result["code"], result["data"]), ("LAND_BLOCKED", {"files": ["note.txt"]}))

    async def test_git_failure_comes_back_with_git_own_words(self):
        agent = await self.agent("甲")
        self.change(agent, "part.txt", "landed\n")
        head = git(self.integration, "rev-parse", "HEAD")
        lock = Path(git(self.integration, "rev-parse", "--absolute-git-dir")) / "index.lock"
        lock.touch()
        self.addCleanup(lock.unlink)
        result = await self.call(LAND, agent)
        self.assertEqual(result["code"], "LAND_FAILED")
        self.assertIn("index.lock", result["data"]["git"])
        self.assertEqual(git(self.integration, "rev-parse", "HEAD"), head)

    async def test_change_review_shows_unlanded_work_without_touching_the_index(self):
        agent = await self.agent("甲")
        queries = AssistantQueries(self.host.store, self.host)
        self.assertEqual(await queries.file_changes(agent.session_id),
                         {"files": [], "added": 0, "removed": 0})
        self.change(agent, "note.txt", "initial\ncommitted\n")
        (agent.worktree / "draft.txt").write_text("one\ntwo\n", encoding="utf-8", newline="\n")
        status = git(agent.worktree, "status", "--porcelain")
        listed = await queries.file_changes(agent.session_id)
        detail = await queries.file_changes(agent.session_id, path="note.txt")
        self.assertEqual([(file["path"], file["status"], file["added"], file["removed"]) for file in listed["files"]],
                         [("draft.txt", "added", 2, 0), ("note.txt", "modified", 1, 0)])
        self.assertEqual((listed["added"], listed["removed"]), (3, 0))
        self.assertEqual(detail["revision"], listed["files"][1]["revision"])
        self.assertEqual([(line["kind"], line["old_line"], line["new_line"], line["text"]) for line in detail["lines"]],
                         [("hunk", None, None, "@@ -1 +1,2 @@"), ("context", 1, 1, "initial\n"),
                          ("add", None, 2, "committed\n")])
        self.assertIsNone(detail["next_offset"])
        self.assertIsNone(await queries.file_changes(agent.session_id, path="missing.txt"))
        self.assertEqual(git(agent.worktree, "status", "--porcelain"), status)
        self.change(agent, "draft.txt", "one\ntwo\n")
        await self.call(LAND, agent)
        self.assertEqual((await queries.file_changes(agent.session_id))["files"], [])
