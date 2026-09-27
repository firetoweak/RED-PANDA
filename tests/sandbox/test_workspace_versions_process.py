import asyncio
import os
from pathlib import Path
import subprocess

import pytest

from helperme.sandbox.versions import UnknownWorkspaceVersion, WorkspaceVersions


pytestmark = pytest.mark.process


def git(root: Path, *args: str):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def test_restore_is_reversible_and_preserves_user_git(tmp_path):
    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        git(root, "init")
        (root / "file.txt").write_bytes(b"original\r\n")
        git(root, "add", ".")
        git(root, "-c", "user.name=Test", "-c", "user.email=test@local",
            "commit", "-m", "user commit")
        (root / "file.txt").write_bytes(b"staged\r\n")
        git(root, "add", ".")
        user_head = git(root, "rev-parse", "HEAD")
        user_index = (root / ".git" / "index").read_bytes()
        (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (root / ".gitattributes").write_text("*.txt text eol=lf\n", encoding="utf-8")
        (root / ".git" / "info" / "exclude").write_text("local-only\n", encoding="utf-8")
        (root / "local-only").write_text("do not record")
        (root / "ignored").mkdir()
        (root / "ignored" / "keep.txt").write_text("ignored")
        versions = WorkspaceVersions(root, tmp_path / "versions")
        baseline = await versions.record()
        tracked = git(root, f"--git-dir={versions.repository}", "ls-tree", "-r", "--name-only", baseline)
        assert b"local-only" not in tracked
        assert await versions.record() == baseline
        (root / "file.txt").write_bytes(b"changed\r\n")
        (root / "new.txt").write_text("new")
        changed = await versions.record()
        (root / "manual.txt").write_text("manual")
        result = await versions.restore(baseline)
        assert (root / "file.txt").read_bytes() == b"staged\r\n"
        assert not (root / "new.txt").exists()
        assert not (root / "manual.txt").exists()
        assert result.version not in (baseline, changed, result.before_version)
        assert (root / "ignored" / "keep.txt").read_text() == "ignored"
        await versions.restore(result.before_version)
        assert (root / "manual.txt").read_text() == "manual"
        assert (root / "new.txt").read_text() == "new"
        assert (root / "file.txt").read_bytes() == b"changed\r\n"
        assert git(root, "rev-parse", "HEAD") == user_head
        assert (root / ".git" / "index").read_bytes() == user_index
    asyncio.run(scenario())


def test_plain_directory_and_nested_repository_files(tmp_path):
    async def scenario():
        root = tmp_path / "plain"
        root.mkdir()
        nested = root / "nested"
        nested.mkdir()
        git(nested, "init")
        (nested / "file").write_text("first")
        home = root / "home"
        home.mkdir()
        (home / "journal").write_text("first fact")
        versions = WorkspaceVersions(root, home / "versions", excluded_roots=(home,))
        initial = await versions.record()
        (nested / "file").write_text("second")
        (home / "journal").write_text("new fact")
        await versions.restore(initial)
        assert (nested / "file").read_text() == "first"
        assert (home / "journal").read_text() == "new fact"
        assert (nested / ".git" / "HEAD").is_file()
        with pytest.raises(UnknownWorkspaceVersion):
            await versions.restore("0" * 40)
    asyncio.run(scenario())


def test_ignored_directories_are_not_read_but_recorded_ones_must_be(tmp_path, monkeypatch):
    """剪枝在前，失败在后：忽略的目录不必可读，要记录的读不动就是记不成。"""
    async def scenario():
        root = tmp_path / "workspace"
        (root / "cache").mkdir(parents=True)
        (root / "src").mkdir()
        (root / ".gitignore").write_text("cache/\n", encoding="utf-8")
        denied = {root / "cache"}
        scandir = os.scandir

        def guarded(path=".", *args, **kwargs):
            if Path(path) in denied:
                raise PermissionError(13, "denied", str(path))
            return scandir(path, *args, **kwargs)

        monkeypatch.setattr(os, "scandir", guarded)
        versions = WorkspaceVersions(root, tmp_path / "versions")
        version = await versions.record()
        tracked = git(root, f"--git-dir={versions.repository}", "ls-tree", "-r", "--name-only", version)
        assert sorted(tracked.split()) == [b".gitignore"]
        denied.add(root / "src")
        with pytest.raises(OSError):
            await versions.record()
    asyncio.run(scenario())


def test_instances_share_one_track_and_corruption_is_not_an_environment_failure(tmp_path):
    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        (root / "中文 space.txt").write_text("content", encoding="utf-8")
        first = WorkspaceVersions(root, tmp_path / "versions")
        second = WorkspaceVersions(root, tmp_path / "versions")
        a, b = await asyncio.gather(first.record(), second.record())
        assert a == b
        (root / "中文 space.txt").write_text("changed", encoding="utf-8")
        newer = await second.record()
        parent = git(root, f"--git-dir={first.repository}", "rev-parse", f"{newer}^")
        assert parent.decode().strip() == a
        (first.repository / "HEAD").write_text("corrupt")
        with pytest.raises(RuntimeError, match="workspace Git"):
            await first.record()
    asyncio.run(scenario())


def test_child_ref_and_restore_do_not_advance_parent_head(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        (root / "file").write_text("base")
        parent = WorkspaceVersions(root, tmp_path / "versions")
        child_root = tmp_path / "child"
        ref = "refs/subagents/one"
        baseline = await parent.fork(child_root, ref)
        child = WorkspaceVersions(child_root, parent.storage, ref=ref, ignore_root=root)
        (child_root / "file").write_text("child")
        child_version = await child.record()
        assert child_version != baseline
        assert await parent.record() == baseline
        (root / "file").write_text("parent")
        parent_version = await parent.record()
        assert await parent.fork(child_root, ref) == baseline
        assert (child_root / "file").read_text() == "child"
        await child.restore(baseline)
        assert await parent.record() == parent_version
        assert (root / "file").read_text() == "parent"
        with pytest.raises(UnknownWorkspaceVersion):
            await child.restore(parent_version)
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "nt", reason="Windows Git 路径长度契约")
def test_internal_git_handles_long_child_ref_lock_paths(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        (root / "file").write_text("base")
        parent = WorkspaceVersions(root, tmp_path / ("s" * 64))
        ref = "refs/subagents/" + "a" * 64
        assert len(str(parent.repository)) < 260
        assert len(str(parent.repository / (ref + "-base.lock"))) > 260
        await parent.fork(tmp_path / "child", ref)
        assert (tmp_path / "child" / "file").read_text() == "base"
    asyncio.run(scenario())


def test_child_uses_parent_local_excludes(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        git(root, "init")
        (root / ".git" / "info" / "exclude").write_text("local-only\n")
        (root / "file").write_text("base")
        parent = WorkspaceVersions(root, tmp_path / "versions")
        child_root = tmp_path / "child"
        ref = "refs/subagents/one"
        await parent.fork(child_root, ref)
        assert not (child_root / ".git").exists()
        (child_root / "local-only").write_text("ignore")
        child = WorkspaceVersions(child_root, parent.storage, ref=ref, ignore_root=root)
        version = await child.record()
        tracked = git(root, f"--git-dir={parent.repository}", "ls-tree", "-r", "--name-only", version)
        assert tracked.splitlines() == [b"file"]
    asyncio.run(scenario())


def test_merge_preserves_parent_edits_and_compare_can_select_files(tmp_path):
    async def scenario():
        root = tmp_path / "parent"
        root.mkdir()
        (root / "one").write_text("base")
        (root / "two").write_text("base")
        parent = WorkspaceVersions(root, tmp_path / "versions")
        child_root = tmp_path / "child"
        ref = "refs/subagents/one"
        base = await parent.fork(child_root, ref)
        (root / "two").write_text("parent")
        (child_root / "one").write_text("child")
        (child_root / "new").write_text("new")
        child = WorkspaceVersions(child_root, parent.storage, ref=ref, ignore_root=root)
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
        parent = WorkspaceVersions(root, tmp_path / "versions")
        child_root = tmp_path / "child"
        ref = "refs/subagents/one"
        base = await parent.fork(child_root, ref)
        (root / "file").write_text("parent\n")
        (child_root / "file").write_text("child\n")
        child = WorkspaceVersions(child_root, parent.storage, ref=ref, ignore_root=root)
        version = await child.record()
        assert await parent.merge(base, version) == ("file",)
        assert (root / "file").read_text() == "parent\n"
        resolving_root = tmp_path / "resolving"
        await parent.fork(resolving_root, "refs/subagents/two", conflict_from=(base, version))
        assert "<<<<<<<" in (resolving_root / "file").read_text()
        assert (root / "file").read_text() == "parent\n"
    asyncio.run(scenario())
