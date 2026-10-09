"""RED PANDA adapter against the real native file view, in serial order."""
import asyncio
import os
import subprocess
import threading
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import pytest

from redpanda.sandbox.versions import INITIAL, WorkspaceVersions, native_executable, operation_id

pytestmark = [pytest.mark.process, pytest.mark.skipif(
    os.name != "nt" or not native_executable().is_file(),
    reason="需要已构建的 Windows sandbox 与 WinFsp",
)]


def backend(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return WorkspaceVersions(root, tmp_path / "store")


def test_record_does_not_enumerate_workspace(tmp_path):
    async def scenario():
        view = backend(tmp_path)
        with patch("os.scandir", side_effect=AssertionError("workspace enumeration")):
            assert await view.record() == INITIAL
    asyncio.run(scenario())


def test_subagent_review_merge_is_captured_by_parent_projection(tmp_path):
    from redpanda.assistant.subagent.workspace import ChildWorkspaceReview, child_layout
    from redpanda.paths import RedPandaHome
    from redpanda.sandbox.worktrees import ReviewWorktrees
    async def scenario():
        view = backend(tmp_path)
        logical = view.root / "file"
        logical.write_text("base")
        home = RedPandaHome(tmp_path / "home")
        business = ReviewWorktrees(view.root, tmp_path / "reviews")
        child_root, ref = child_layout(home, "parent", "child")
        await business.fork(child_root, ref)
        child = ReviewWorktrees(child_root, business.storage, ref=ref, ignore_root=view.root)
        (child.root / "file").write_text("child")
        version = await child.record()
        async def snapshot(session_id): return []
        async def transport(operation, session_id, arguments):
            assert (operation, session_id, arguments) == ("child_workspace_version", "child", {"parent_session_id": "parent"})
            return {"ok": True, "version": version}
        review = ChildWorkspaceReview(SimpleNamespace(snapshot=snapshot), "parent", business, home, transport, view)
        intent = SimpleNamespace(command_id="delegate", child_session_id="child")
        with patch("redpanda.assistant.subagent.workspace.project_delegate_intents", return_value=[intent]):
            compared = await view.execute(operation_id("parent", "compare"), lambda: review.review("delegate", ["file"]))
            assert compared["ok"] and "+child" in compared["data"]["diff"]
            merged = await view.execute(operation_id("parent", "merge"), lambda: review.review("delegate", merge=True))
            assert merged["ok"]
        assert logical.read_text() == "child"
        (view.root / "user").write_text("human")
        await view.restore(INITIAL, identity=operation_id("parent", "undo"), policy="original")
        assert logical.read_text() == "base"
        assert (view.root / "user").read_text() == "human"
    asyncio.run(scenario())


@pytest.mark.parametrize("policy,expected", [("original", "A-U"), ("preserve", "H-U")])
def test_command_effects_policies_and_retry(tmp_path, policy, expected):
    async def scenario():
        view = backend(tmp_path)
        logical = view.root / "code.txt"
        logical.write_text("A-Z", encoding="utf-8")
        (view.root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        identity = operation_id("s", "edit")
        calls = []
        async def edit():
            calls.append(1)
            native = view.native_path(logical)
            native.write_text("X-Z", encoding="utf-8")
            result = await asyncio.to_thread(subprocess.run,
                ["powershell.exe", "-NoProfile", "-Command", "[IO.File]::WriteAllText('ignored.txt', 'command')"],
                cwd=native.parent, capture_output=True, check=True)
            return {"ok": True, "returncode": result.returncode}
        expected_result = await view.execute(identity, edit)
        assert await view.execute(identity, edit) == expected_result
        assert calls == [1]
        assert (view.root / "ignored.txt").read_text() == "command"
        logical.write_text("H-U", encoding="utf-8")
        user = view.root / "user.txt"
        user.write_text("human")
        restored = operation_id("s", "restore")
        result = await view.restore(INITIAL, identity=restored, policy=policy)
        assert logical.read_text() == expected
        assert not (view.root / "ignored.txt").exists()
        assert user.read_text() == "human"
        assert await view.restore(INITIAL, identity=restored, policy=policy) == result
        assert logical.read_text() == expected
        if policy == "original":
            await view.restore(identity, identity=operation_id("s", "undo"), policy="original")
            assert logical.read_text() == "H-U"
            assert (view.root / "ignored.txt").read_text() == "command"
    asyncio.run(scenario())


def test_unknown_execution_never_publishes_or_reexecutes(tmp_path):
    async def scenario():
        view = backend(tmp_path)
        identity = operation_id("s", "broken")
        async def broken():
            view.native_path(view.root / "partial").write_text("candidate")
            raise LookupError("provider failed")
        with pytest.raises(LookupError, match="provider failed"):
            await view.execute(identity, broken)
        assert not (view.root / "partial").exists()
        assert await view.record() == INITIAL
        with pytest.raises(RuntimeError, match="explicit recovery"):
            await view.execute(identity, broken)
        assert not (view.root / "partial").exists()
    asyncio.run(scenario())


def test_two_owners_serialize_and_read_preceding_publication(tmp_path):
    async def scenario():
        first = backend(tmp_path)
        second = WorkspaceVersions(first.root, first.storage)
        logical = first.root / "ordered"
        entered, release = asyncio.Event(), asyncio.Event()
        async def one():
            first.native_path(logical).write_text("one")
            entered.set()
            await release.wait()
            return {"ok": True}
        async def two():
            assert second.native_path(logical).read_text() == "one"
            second.native_path(logical).write_text("two")
            return {"ok": True}
        task_one = asyncio.create_task(first.execute(operation_id("s1", "one"), one))
        await entered.wait()
        task_two = asyncio.create_task(second.execute(operation_id("s2", "two"), two))
        release.set()
        await asyncio.gather(task_one, task_two)
        assert logical.read_text() == "two"
    asyncio.run(scenario())


def test_noop_restore_retry_cannot_undo_later_commands(tmp_path):
    async def scenario():
        view = backend(tmp_path)
        identity = operation_id("s", "noop")
        expected = await view.restore(INITIAL, identity=identity)
        async def later():
            view.native_path(view.root / "later").write_text("new")
            return {"ok": True}
        await view.execute(operation_id("s", "later"), later)
        assert await view.restore(INITIAL, identity=identity) == expected
        assert (view.root / "later").read_text() == "new"
        with pytest.raises(ValueError, match="identity was reused"):
            await view.restore(INITIAL, identity=identity, policy="original")
    asyncio.run(scenario())


@pytest.mark.parametrize("deep_store", [False, True])
def test_discovery_paths_are_logical_and_git_reads_projected_files(tmp_path, deep_store):
    from redpanda.assistant.builtin_tools import build_builtin_tools
    from tests.fixtures.workspaces import workspace_record
    async def scenario():
        view = backend(tmp_path)
        if deep_store:
            storage = tmp_path / ("s" * (235 - len(str(tmp_path)) - 1))
            view = WorkspaceVersions(view.root, storage)
            assert len(str(storage / "mount" / ".git" / "objects" / "00" / ("a" * 38))) > 260
        code = view.root / "main.py"
        code.write_text("print('before')\n")
        def git(*args):
            subprocess.run(["git", "-C", str(view.root), *args], capture_output=True, check=True)
        git("init"); git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@local", "commit", "-m", "baseline")
        tools = await build_builtin_tools(workspace_record(view.root), sandbox=view)
        async def inspect():
            view.native_path(code).write_text("print('after')\n")
            found = await tools.execute("glob", {"pattern": "*.py"})
            assert found["ok"] and [item["path"] for item in found["data"]["matches"]] == ["main.py"], found
            assert found["data"]["matches"][0]["location"]["path"] == code.as_uri()
            search = await tools.execute("grep", {"query": "after", "path": "."})
            assert search["ok"] and search["data"]["hits"], search
            changes = await tools.execute("get_changes", {})
            assert changes["ok"] and "+print('after')" in changes["data"]["diff"], changes
            assert changes["data"]["repository_location"]["path"] == view.root.as_uri()
            return {"ok": True}
        await view.execute(operation_id("s", "discovery"), inspect)
    asyncio.run(scenario())


def test_internal_metadata_replace_tolerates_a_brief_reader_and_exposes_persistent_lock(tmp_path):
    from redpanda.sandbox.file_view import publication
    path = tmp_path / "metadata.json"
    publication.atomic(path, {"value": "before"})
    held = publication.native.open_file(path)
    blocked = threading.Event()
    replace = os.replace
    def observe(source, destination):
        try:
            return replace(source, destination)
        except OSError:
            blocked.set()
            raise
    def reader():
        assert blocked.wait(2)
        held.close()
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        with patch("redpanda.sandbox.file_view.publication.os.replace", observe):
            publication.atomic(path, {"value": "after"})
    finally:
        thread.join(timeout=2)
        held.close()
    assert publication.load(path) == {"value": "after"}
    with publication.native.open_file(path):
        with pytest.raises(PermissionError):
            publication.atomic(path, {"value": "refused"})
    assert publication.load(path) == {"value": "after"}


def test_native_history_pointer_handles_a_brief_reader(tmp_path):
    from redpanda.sandbox.file_view import Client, publication
    root = tmp_path / "project"
    root.mkdir()
    with Client(native_executable(), tmp_path / "store", root) as client:
        for name in ("first", "second"):
            mount = Path(client.begin(name)["mount"])
            (mount / "file").write_text(name)
            assert client.request("finish")["status"] == "sealed"
            if name == "first":
                assert client.request("accept", command_id=name)["status"] == "accepted"
                continue
            held = publication.native.open_file(client.store / "HEAD")
            release = threading.Timer(0.035, held.close)
            release.start()
            try:
                assert client.request("accept", command_id=name)["status"] == "accepted"
            finally:
                release.join()
                held.close()
