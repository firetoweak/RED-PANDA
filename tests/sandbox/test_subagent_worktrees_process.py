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
def test_materialization_retries_preserve_started_child(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        (root / "file").write_text("base")
        parent = ReviewWorktrees(root, tmp_path / ("s" * 64))
        base = await parent.snapshot()
        await parent.materialize(tmp_path / "child", base)
        (tmp_path / "child" / "file").write_text("started")
        await parent.materialize(tmp_path / "child", base)
        assert (tmp_path / "child" / "file").read_text() == "started"
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
        base = await parent.snapshot()
        await parent.materialize(child_root, base)
        child = ReviewWorktrees(child_root, parent.storage, ignore_root=root)
        (child.root / relative).write_text("child")
        version = await child.record(base, (relative.as_posix(),))
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
        base = await parent.snapshot()
        await parent.materialize(child_root, base)
        assert not (child_root / ".git").exists()
        (child_root / "local-only").write_text("ignore")
        (child_root / ".git").mkdir()
        (child_root / ".git" / "config").write_text("local metadata")
        child = ReviewWorktrees(child_root, parent.storage, ignore_root=root)
        version = await child.record(base, ("local-only", ".git/config"))
        tracked = parent._git(None, f"--git-dir={parent.repository.as_posix().removeprefix('//?/')}",
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
        base = await parent.snapshot()
        await parent.materialize(child_root, base)
        (root / "two").write_text("parent")
        (child_root / "one").write_text("child")
        (child_root / "new").write_text("new")
        child = ReviewWorktrees(child_root, parent.storage, ignore_root=root)
        version = await child.record(base, ("one", "new"))
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
        base = await parent.snapshot()
        await parent.materialize(child_root, base)
        (root / "file").write_text("parent\n")
        (child_root / "file").write_text("child\n")
        child = ReviewWorktrees(child_root, parent.storage, ignore_root=root)
        version = await child.record(base, ("file",))
        assert await parent.merge(base, version) == ("file",)
        assert (root / "file").read_text() == "parent\n"
        resolving_root = tmp_path / "resolving"
        current = await parent.snapshot()
        seed = await parent.seed(current, child, {"base": base, "version": version})
        await parent.materialize(resolving_root, seed)
        assert "<<<<<<<" in (resolving_root / "file").read_text()
        assert (root / "file").read_text() == "parent\n"
    asyncio.run(scenario())


def test_changed_ignore_rules_filter_baseline_names_without_rehashing_contents(tmp_path):
    from unittest.mock import patch
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        (root / "untouched").write_bytes(b"U" * 65536)
        parent = ReviewWorktrees(root, tmp_path / "versions")
        base = await parent.snapshot()
        (root / ".gitignore").write_text("untouched\n")
        hashed = []
        original = ReviewWorktrees._hash
        def measured(backend, index, included):
            hashed.extend(included)
            return original(backend, index, included)
        with patch.object(ReviewWorktrees, "_hash", measured):
            version = await parent.record(base, (".gitignore",))
        assert hashed == [b".gitignore"]
        assert (await parent.compare(base, version))["files"] == [
            {"status": "A", "path": ".gitignore"}, {"status": "D", "path": "untouched"},
        ]
    asyncio.run(scenario())
