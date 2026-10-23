"""Real Git creation owns Agent worktrees without changing the human checkout."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time
import shlex
import signal
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import AsyncMock, patch

import pytest

from redpanda.assistant.host.agent_projects import ProjectLinkRefused
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome


pytestmark = pytest.mark.process


def test_internal_git_descendants_exit_when_the_owner_is_killed(tmp_path):
    """An internal Git invocation must not outlive the process recovering its work."""
    script = tmp_path / "writer.py"
    script.write_text("import os,time\nfrom pathlib import Path\n"
                      "Path('writer.pid').write_text(str(os.getpid()))\n"
                      "while True:\n with Path('writes').open('a') as f: f.write('write\\n')\n time.sleep(.03)\n")
    command = "!" + shlex.join([Path(sys.executable).as_posix(), script.as_posix()])
    code = ("import os,sys\nfrom pathlib import Path\n"
            "from redpanda.assistant.agent_git import Git\n"
            "root=Path(sys.argv[1]); (root/'owner.pid').write_text(str(os.getpid()))\n"
            "Git(os.environ).run(root,'-c',sys.argv[2],'owned-writer')\n")
    owner = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), f"alias.owned-writer={command}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "writes").exists():
            if owner.poll() is not None:
                raise AssertionError(owner.stderr.read().decode(errors="replace"))
            assert time.monotonic() < deadline
            time.sleep(.03)
        pid = int((tmp_path / "owner.pid").read_text())
        os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        owner.wait(timeout=20)
        # Allow the OS Job / Linux subreaper to drain after the control owner dies.
        time.sleep(.5)
        before = (tmp_path / "writes").read_bytes()
        time.sleep(.2)
        assert (tmp_path / "writes").read_bytes() == before
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=20)
        owner.stderr.close()


def tree(agent) -> Path:
    """The Agent's worktree of the one project these tests link."""
    linked, = agent.projects
    return Path(linked.worktree)


def git(root, *arguments):
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@local", *arguments],
        check=True, capture_output=True, text=True, encoding="utf-8", env=environment,
    ).stdout.strip()


class AgentWorktreeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.project = self.root / "项目 with spaces"
        self.project.mkdir()
        git(self.project, "init")
        (self.project / "note.txt").write_text("initial\n", encoding="utf-8")
        git(self.project, "add", "note.txt")
        git(self.project, "commit", "-m", "initial")
        self.home = RedPandaHome(self.root / "home")
        self.registry = WorkspaceRegistry.load(self.home.workspaces_path)
        self.workspace = self.registry.register_path(self.project)
        self.store = SessionStore(self.home.runtime_sessions_root)
        # These tests own Git creation; waking the new Agent is covered with a real Worker elsewhere.
        activation = patch.object(HostSupervisor, "request", AsyncMock())
        activation.start()
        self.addCleanup(activation.stop)
        self.host = self.new_host()

    def new_host(self):
        return HostSupervisor(self.store, None, self.home, None, llm=None, workspaces=self.registry)

    async def asyncTearDown(self):
        await self.host.close()

    async def create(self, name="小熊猫", workspace=None):
        return await self.host.create_agent(name=name, workspace_id=(workspace or self.workspace).workspace_id)

    async def test_restart_and_new_agents_use_integration_without_resetting_human_checkout(self):
        (self.project / "note.txt").write_text("staged\n", encoding="utf-8")
        git(self.project, "add", "note.txt")
        (self.project / "note.txt").write_text("human dirty\n", encoding="utf-8")
        (self.project / "untracked.txt").write_text("human only\n", encoding="utf-8")
        before = (git(self.project, "status", "--porcelain=v1"),
                  git(self.project, "diff", "--cached"), git(self.project, "rev-parse", "HEAD"))
        first = await self.create()
        integration = self.host.agent_worktrees.integration(self.workspace.workspace_id)
        self.assertEqual((tree(first) / "note.txt").read_text(), "initial\n")
        self.assertFalse((tree(first) / "untracked.txt").exists())
        self.assertEqual(git(tree(first), "symbolic-ref", "--short", "HEAD"), first.projects[0].branch)
        (integration.path / "shared.txt").write_text("integrated\n", encoding="utf-8")
        git(integration.path, "add", "shared.txt")
        git(integration.path, "commit", "-m", "advance integration")
        integrated = git(integration.path, "rev-parse", "HEAD")
        (tree(first) / "private.txt").write_text("agent dirty\n", encoding="utf-8")
        await self.host.close()
        self.host = self.new_host()
        second = await self.create("另一个")
        self.assertEqual(git(tree(second), "rev-parse", "HEAD"), integrated)
        self.assertEqual((tree(second) / "shared.txt").read_text(), "integrated\n")
        self.assertFalse((tree(second) / "private.txt").exists())
        self.assertEqual((tree(first) / "private.txt").read_text(), "agent dirty\n")
        self.assertEqual(await self.host.agents.resolve(first.name), first)
        self.assertEqual(self.registry.workspaces, (self.workspace,))
        after = (git(self.project, "status", "--porcelain=v1"),
                 git(self.project, "diff", "--cached"), git(self.project, "rev-parse", "HEAD"))
        self.assertEqual(after, before)
        self.assertEqual((self.project / "note.txt").read_text(), "human dirty\n")
        self.assertEqual(len(git(self.project, "worktree", "list", "--porcelain").split("worktree ")) - 1, 4)

    async def test_invalid_projects_do_not_create_an_agent_or_initialize_git(self):
        plain = self.root / "plain"
        plain.mkdir()
        empty = self.root / "empty"
        empty.mkdir()
        git(empty, "init")
        bare = self.root / "bare.git"
        git(self.root, "init", "--bare", str(bare))
        subdirectory = self.project / "subdirectory"
        subdirectory.mkdir()
        for root, message in ((plain, "Git 仓库"), (empty, "尚无提交"),
                              (bare, "裸仓库"), (subdirectory, "根目录")):
            with self.subTest(root=root):
                workspace = self.registry.create(task_root=root)
                with self.assertRaisesRegex(ProjectLinkRefused, message):
                    await self.create(workspace=workspace)
        self.assertFalse((plain / ".git").exists())
        self.assertEqual(await self.host.agents.list(), ())
        self.assertFalse(self.host.agent_worktrees.root.exists())

    async def test_detached_head_can_seed_a_project_and_later_human_head_is_irrelevant(self):
        git(self.project, "checkout", "--detach")
        first = await self.create()
        git(self.project, "checkout", "--orphan", "human-new-history")
        second = await self.create("第二位")
        self.assertEqual(git(tree(first), "rev-parse", "HEAD"), git(tree(second), "rev-parse", "HEAD"))
        self.assertEqual(git(self.project, "symbolic-ref", "--short", "HEAD"), "human-new-history")

    @unittest.skipUnless(sys.platform == "linux", "需要 Linux 的独立 tmpfs 挂载")
    async def test_non_repository_at_a_filesystem_boundary_does_not_create_an_agent(self):
        with TemporaryDirectory(dir="/dev/shm") as directory:
            root = Path(directory).resolve()
            workspace = self.registry.create(task_root=root)
            with self.assertRaisesRegex(ProjectLinkRefused, "Git 仓库"):
                await self.create(workspace=workspace)
            self.assertFalse((root / ".git").exists())
        self.assertEqual(await self.host.agents.list(), ())
        self.assertFalse(self.host.agent_worktrees.root.exists())

    async def test_data_directory_inside_the_human_checkout_is_rejected(self):
        home = RedPandaHome(self.project / "app-data")
        host = HostSupervisor(SessionStore(home.runtime_sessions_root), None, home, None,
                              llm=None, workspaces=self.registry)
        try:
            with self.assertRaisesRegex(ProjectLinkRefused, "数据目录"):
                await host.create_agent(name="小熊猫", workspace_id=self.workspace.workspace_id)
            self.assertFalse(host.agent_worktrees.root.exists())
            self.assertEqual(await host.agents.list(), ())
        finally:
            await host.close()

    async def test_broken_git_metadata_is_not_downgraded_to_an_invalid_project(self):
        linked = self.root / "broken-linked-worktree"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: missing-metadata\n", encoding="utf-8")
        workspace = self.registry.create(task_root=linked)
        expected = subprocess.run(["git", "-C", str(linked), "rev-parse", "--is-inside-work-tree"],
                                  capture_output=True, env=self.host.agent_worktrees.git.environment)
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            await self.create(workspace=workspace)
        self.assertEqual(raised.exception.returncode, expected.returncode)
        self.assertEqual(raised.exception.stderr, expected.stderr)
        self.assertEqual(await self.host.agents.list(), ())

    async def test_failed_identity_write_removes_only_the_new_agent_worktree_and_branch(self):
        error = OSError("journal unavailable")
        with patch.object(self.store, "create", new=AsyncMock(side_effect=error)):
            with self.assertRaises(OSError) as raised:
                await self.create()
        self.assertIs(raised.exception, error)
        integration = self.host.agent_worktrees.integration(self.workspace.workspace_id)
        self.assertTrue(integration.path.is_dir())
        self.assertEqual(list(integration.path.parent.iterdir()), [integration.path])
        branches = git(self.project, "for-each-ref", "--format=%(refname)",
                       f"refs/heads/{self.host.agent_worktrees.namespace}")
        self.assertEqual(branches, f"refs/heads/{integration.branch}")
        self.assertEqual(await self.host.agents.list(), ())

    async def test_git_checkout_failure_keeps_the_error_and_removes_its_new_ref(self):
        await self.create()
        before = git(self.project, "for-each-ref", "--format=%(refname)")
        original = self.host.agent_worktrees._git
        error = OSError("checkout denied")

        def fail_checkout(root, *arguments, **kwargs):
            if arguments[:2] == ("worktree", "add"):
                raise error
            return original(root, *arguments, **kwargs)

        with patch.object(self.host.agent_worktrees, "_git", side_effect=fail_checkout):
            with self.assertRaises(OSError) as raised:
                await self.create("第二位")
        self.assertIs(raised.exception, error)
        self.assertEqual(git(self.project, "for-each-ref", "--format=%(refname)"), before)
        self.assertEqual(len(await self.host.agents.list()), 1)

    async def test_required_checkout_filter_failure_does_not_leave_an_integration_worktree(self):
        (self.project / ".gitattributes").write_text("note.txt filter=unavailable\n", encoding="utf-8")
        git(self.project, "add", ".gitattributes")
        git(self.project, "commit", "-m", "checkout filter")
        git(self.project, "config", "filter.unavailable.smudge", "redpanda-filter-that-does-not-exist")
        git(self.project, "config", "filter.unavailable.required", "true")
        before = git(self.project, "worktree", "list", "--porcelain")
        with self.assertRaises(subprocess.CalledProcessError):
            await self.create()
        integration = self.host.agent_worktrees.integration(self.workspace.workspace_id)
        self.assertFalse(integration.path.exists())
        self.assertEqual(git(self.project, "worktree", "list", "--porcelain"), before)
        self.assertEqual(git(self.project, "for-each-ref", "--format=%(refname)",
                             f"refs/heads/{self.host.agent_worktrees.namespace}"), "")
        self.assertEqual(await self.host.agents.list(), ())

    async def test_cancellation_waits_for_git_and_the_identity_fact_before_releasing_creation(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        original = self.host.agent_worktrees.link

        def delayed(project, session_id, path):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release Git creation")
            return original(project, session_id, path)

        with patch.object(self.host.agent_worktrees, "link", side_effect=delayed):
            creation = asyncio.create_task(self.create())
            async with asyncio.timeout(10):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            creation.cancel()
            await asyncio.sleep(0)
            creation.cancel()
            await asyncio.sleep(0)
            self.assertFalse(creation.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await creation
        agent = await self.host.agents.resolve("小熊猫")
        self.assertTrue(tree(agent).is_dir())
        self.assertEqual(git(tree(agent), "symbolic-ref", "--short", "HEAD"), agent.projects[0].branch)

    async def test_repository_hooks_and_inherited_git_environment_cannot_redirect_creation(self):
        hook = self.project / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\necho unexpected > hook-ran.txt\n", encoding="utf-8")
        hook.chmod(0o755)
        environment = {**os.environ, "GIT_DIR": str(self.root / "wrong.git"),
                       "GIT_WORK_TREE": str(self.root / "wrong"), "GIT_INDEX_FILE": str(self.root / "wrong-index")}
        await self.host.close()
        self.host = HostSupervisor(self.store, None, self.home, None, llm=None,
                                   workspaces=self.registry, command_environment=environment)
        agent = await self.create()
        integration = self.host.agent_worktrees.integration(self.workspace.workspace_id)
        for path in (self.project, tree(agent), integration.path):
            self.assertFalse((path / "hook-ran.txt").exists())
        self.assertFalse((self.root / "wrong-index").exists())

    async def test_two_data_roots_have_distinct_integration_branches(self):
        first = await self.create()
        home = RedPandaHome(self.root / "other-home")
        store = SessionStore(home.runtime_sessions_root)
        other = HostSupervisor(store, None, home, None, llm=None, workspaces=self.registry)
        try:
            second = await other.create_agent(name="小熊猫", workspace_id=self.workspace.workspace_id)
            self.assertNotEqual(tree(first), tree(second))
            self.assertNotEqual(self.host.agent_worktrees.integration(self.workspace.workspace_id).branch,
                                other.agent_worktrees.integration(self.workspace.workspace_id).branch)
        finally:
            await other.close()

    async def test_changed_integration_checkout_is_not_silently_replaced(self):
        await self.create()
        integration = self.host.agent_worktrees.integration(self.workspace.workspace_id)
        git(integration.path, "checkout", "--detach")
        with self.assertRaises(subprocess.CalledProcessError):
            await self.create("第二位")
        self.assertEqual(git(integration.path, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(len(await self.host.agents.list()), 1)
