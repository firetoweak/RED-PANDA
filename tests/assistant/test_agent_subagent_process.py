"""An Agent's delegated tasks are branches of its own branch, merged back without approval."""
from __future__ import annotations

import asyncio
from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pytest

from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.subagent.subagent import project_delegate_intents
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from redpanda.paths import RedPandaHome
from redpanda.runtime import SqliteJournal
from tests.assistant.test_agent_worktrees_process import git, tree
from tests.fixtures.session_worker import ProcessLlm, config_for, is_subagent_request


pytestmark = pytest.mark.process


def calls(messages):
    return [(call["id"], call["function"]["name"]) for message in messages
            for call in message.get("tool_calls", ())]


class DelegatingLlm(ProcessLlm):
    """Parent delegates, compares and merges; the child writes a file and reports."""

    async def chat(self, messages, model, *, tools=None, **kwargs):
        made = calls(messages)
        names = [name for _, name in made]
        text = json.dumps(messages, ensure_ascii=False)
        call = None
        if is_subagent_request(tools):
            if "write_file" not in names:
                call = ToolCall("w", "write_file", json.dumps({"path": "child.txt", "content": "from child\n"}))
            else:
                return LLMCallResult(LLMResponse(content="CHILD_DONE 写了 child.txt"), LLMUsage(input_tokens=1, output_tokens=1))
        elif "DELEGATE_WORK" in text:
            delegated = next((call_id for call_id, name in made if name == "delegate"), None)
            if delegated is None:
                project = next(json.loads(message["content"])["data"]["project"] for message in messages
                               if '"bot.project_linked"' in str(message.get("content")))
                call = ToolCall("d", "delegate", json.dumps({"task": "在 child.txt 写入 from child", "project": project}))
            elif "CHILD_DONE" in text and "compare_subagent" not in names:
                call = ToolCall("c", "compare_subagent", json.dumps({"tool_call_id": delegated}))
            elif "compare_subagent" in names and "merge_subagent" not in names:
                call = ToolCall("m", "merge_subagent", json.dumps({"tool_call_id": delegated}))
            elif "merge_subagent" in names and "reclaim" not in names:
                child = next(json.loads(message["content"])["data"]["child_session_id"] for message in messages
                             if message["role"] == "tool" and '"DELEGATED"' in str(message["content"]))
                call = ToolCall("end", "reclaim", json.dumps({"child_session_id": child, "reason": "验收与合入完成"}))
        if call is None:
            return LLMCallResult(LLMResponse(content="done"), LLMUsage(input_tokens=1, output_tokens=1))
        return LLMCallResult(LLMResponse(content="", calls=(call,)), LLMUsage(input_tokens=1, output_tokens=1))


class AgentSubagentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        git(self.project, "init")
        git(self.project, "config", "core.autocrlf", "false")
        (self.project / "note.txt").write_text("initial\n", encoding="utf-8")
        git(self.project, "add", "note.txt")
        git(self.project, "commit", "-m", "initial")
        self.home = RedPandaHome(self.root / "home")
        registry = WorkspaceRegistry.load(self.home.workspaces_path)
        self.workspace = registry.register_path(self.project)
        self.llm = DelegatingLlm(self.root)
        self.host = HostSupervisor(SessionStore(self.home.runtime_sessions_root),
                                   partial(config_for, self.root), self.home, None,
                                   llm=self.llm, workspaces=registry)

    async def asyncTearDown(self):
        await self.host.close()

    async def test_child_works_in_its_own_branch_and_merges_into_the_parent_without_approval(self):
        parent = await self.host.create_agent(name="小熊猫", workspace_id=self.workspace.workspace_id)
        await self.host.select("page", parent.session_id)
        await self.host.receive_user_message(parent.session_id, "DELEGATE_WORK", delivery_id="work")
        async with asyncio.timeout(60):
            while not (tree(parent) / "child.txt").exists():
                self.assertTrue(self.host.failures.empty())
                await asyncio.sleep(0.05)
            await self.host.wait_quiescent(parent.session_id)
        self.assertEqual((tree(parent) / "child.txt").read_text(encoding="utf-8"), "from child\n")
        self.assertEqual(git(tree(parent), "log", "-1", "--format=%an", "--", "child.txt"), "小熊猫")
        self.assertEqual(git(tree(parent), "status", "--porcelain"), "")
        self.assertFalse((self.home.state_root / "workspace_views").exists())
        self.assertFalse((self.home.state_root / "subagent_worktrees").exists())
        self.assertFalse((self.project / "child.txt").exists())
        events = await SqliteJournal(self.host.store.require(parent.session_id)).snapshot(parent.session_id)
        [intent] = project_delegate_intents(events)
        self.assertTrue(self.host.failures.empty())

    async def test_conflicts_never_reach_the_parent_and_a_resolving_task_completes_the_merge(self):
        parent = await self.host.create_agent(name="小熊猫", workspace_id=self.workspace.workspace_id)
        trees = self.host.agent_worktrees
        project = self.host.workspaces.get(self.workspace.workspace_id)
        first, second = f"{parent.session_id}/sub-1", f"{parent.session_id}/sub-2"
        linked, = parent.projects
        child = trees.create_child(project, parent, linked, first)
        self.assertEqual(trees.create_child(project, parent, linked, first), child)
        (child.path / "note.txt").write_text("child\n", encoding="utf-8")
        trees.finish_child(parent, linked.workspace_id, first)
        (tree(parent) / "note.txt").write_text("parent\n", encoding="utf-8")
        git(tree(parent), "commit", "-am", "parent change")
        head = git(tree(parent), "rev-parse", "HEAD")

        files = trees.compare_child(linked, first, ("note.txt",), 0)
        self.assertEqual(files["files"], [{"status": "M", "path": "note.txt"}])
        self.assertIn("+child", files["diff"])
        result, changed = trees.merge_child(parent, linked, first)
        self.assertEqual((result.status, result.files, changed), ("conflict", ("note.txt",), ()))
        self.assertEqual(git(tree(parent), "rev-parse", "HEAD"), head)
        self.assertEqual(git(tree(parent), "status", "--porcelain"), "")

        resolving = trees.create_child(project, parent, linked, second, first)
        self.assertIn("<<<<<<<", (resolving.path / "note.txt").read_text(encoding="utf-8"))
        # Replaying the delegation keeps the merge it is resolving.
        trees.create_child(project, parent, linked, second, first)
        self.assertTrue(trees.git.merging(resolving.path))
        (resolving.path / "note.txt").write_text("parent and child\n", encoding="utf-8")
        trees.finish_child(parent, linked.workspace_id, second)
        self.assertEqual(len(git(resolving.path, "log", "-1", "--format=%P").split()), 2)
        result, changed = trees.merge_child(parent, linked, second)
        self.assertEqual((result.status, changed), ("merged", ("note.txt",)))
        self.assertEqual((tree(parent) / "note.txt").read_text(encoding="utf-8"), "parent and child\n")
        self.assertEqual(trees.merge_child(parent, linked, first)[0].status, "unchanged")
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    async def test_git_failure_while_fixing_a_returned_result_comes_back_with_git_own_words(self):
        parent = await self.host.create_agent(name="小熊猫", workspace_id=self.workspace.workspace_id)
        trees = self.host.agent_worktrees
        task = f"{parent.session_id}/sub-1"
        linked, = parent.projects
        child = trees.create_child(self.host.workspaces.get(self.workspace.workspace_id), parent, linked, task)
        (child.path / "note.txt").write_text("child\n", encoding="utf-8")
        lock = Path(git(child.path, "rev-parse", "--absolute-git-dir")) / "index.lock"
        lock.touch()
        self.assertIn("index.lock", trees.finish_child(parent, linked.workspace_id, task))
        lock.unlink()
        self.assertIsNone(trees.finish_child(parent, linked.workspace_id, task))
        self.assertEqual(git(child.path, "status", "--porcelain"), "")
