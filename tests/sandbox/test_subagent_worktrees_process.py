"""Explicit SubAgent fork/review/merge contracts, separate from sandbox rollback."""
import asyncio
import os
from pathlib import Path
import subprocess
import pytest
from redpanda.sandbox.worktrees import ReviewWorktrees
pytestmark = pytest.mark.process
def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)
@pytest.mark.skipif(os.name != "nt", reason="Windows Git 路径长度契约")
def test_internal_git_handles_long_child_ref_lock_paths(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        (root / "file").write_text("base")
        parent = ReviewWorktrees(root, tmp_path / ("s" * 64))
        ref = "refs/subagents/" + "a" * 64
        assert len(str(parent.repository)) < 260
        assert len(str(parent.repository / (ref + "-base.lock"))) > 260
        await parent.fork(tmp_path / "child", ref)
        assert (tmp_path / "child" / "file").read_text() == "base"
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Windows 长工作树路径契约")
def test_child_review_reads_files_beyond_windows_normal_path_limit(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        parent = ReviewWorktrees(root, tmp_path / "versions")
        relative = Path("d" * 80) / ("e" * 80) / "file"
        source = parent.root / relative
        source.parent.mkdir(parents=True)
        source.write_text("base")
        child_root = tmp_path / "child"
        assert len(str(child_root / relative)) > 260
        ref = "refs/subagents/deep"
        base = await parent.fork(child_root, ref)
        child = ReviewWorktrees(child_root, parent.storage, ref=ref, ignore_root=root)
        (child.root / relative).write_text("child")
        version = await child.record()
        assert await parent.merge(base, version) == ()
        assert source.read_text() == "child"
    asyncio.run(scenario())


def test_child_uses_parent_local_excludes(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        git(root, "init")
        (root / ".git" / "info" / "exclude").write_text("local-only\n")
        (root / "file").write_text("base")
        parent = ReviewWorktrees(root, tmp_path / "versions")
        child_root = tmp_path / "child"
        ref = "refs/subagents/one"
        await parent.fork(child_root, ref)
        assert not (child_root / ".git").exists()
        (child_root / "local-only").write_text("ignore")
        child = ReviewWorktrees(child_root, parent.storage, ref=ref, ignore_root=root)
        version = await child.record()
        tracked = git(root, f"--git-dir={parent.repository.as_posix().removeprefix('//?/')}",
                      "ls-tree", "-r", "--name-only", version)
        assert tracked.splitlines() == [b"file"]
    asyncio.run(scenario())


def test_merge_preserves_parent_edits_and_compare_can_select_files(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        (root / "one").write_text("base")
        (root / "two").write_text("base")
        parent = ReviewWorktrees(root, tmp_path / "versions")
        child_root = tmp_path / "child"
        ref = "refs/subagents/one"
        base = await parent.fork(child_root, ref)
        (root / "two").write_text("parent")
        (child_root / "one").write_text("child")
        (child_root / "new").write_text("new")
        child = ReviewWorktrees(child_root, parent.storage, ref=ref, ignore_root=root)
        version = await child.record()
        listing = await parent.compare(base, version)
        assert listing == {"files": [{"status": "A", "path": "new"}, {"status": "M", "path": "one"}]}
        patch = await parent.compare(base, version, ("one",))
        assert "+child" in patch["diff"] and "diff --git a/new" not in patch["diff"]
        assert await parent.merge(base, version) == ()
        assert (root / "one").read_text() == "child"
        assert (root / "two").read_text() == "parent"
        assert (root / "new").read_text() == "new"
    asyncio.run(scenario())


def test_merge_conflicts_leave_parent_files_intact_and_can_seed_another_child(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        (root / "file").write_text("base\n")
        parent = ReviewWorktrees(root, tmp_path / "versions")
        child_root = tmp_path / "child"
        ref = "refs/subagents/one"
        base = await parent.fork(child_root, ref)
        (root / "file").write_text("parent\n")
        (child_root / "file").write_text("child\n")
        child = ReviewWorktrees(child_root, parent.storage, ref=ref, ignore_root=root)
        version = await child.record()
        assert await parent.merge(base, version) == ("file",)
        assert (root / "file").read_text() == "parent\n"
        resolving_root = tmp_path / "resolving"
        await parent.fork(resolving_root, "refs/subagents/two", conflict_from=(base, version))
        assert "<<<<<<<" in (resolving_root / "file").read_text()
        assert (root / "file").read_text() == "parent\n"
    asyncio.run(scenario())
