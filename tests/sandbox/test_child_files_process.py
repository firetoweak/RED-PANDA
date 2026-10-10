"""Immutable, incremental child-file handoff through real Git and mounted VFS."""
import asyncio
import threading
from unittest.mock import patch

import pytest

from redpanda.paths import RedPandaHome
from redpanda.sandbox.files import ChildFiles, child_root, child_workspace, workspace_files
from redpanda.sandbox.files.operations import INITIAL, WorkspaceRestoreFailed, native_executable, operation_id
from redpanda.sandbox.files.git import ReviewWorktrees
from tests.fixtures.workspaces import workspace_record

pytestmark = [pytest.mark.process, pytest.mark.skipif(
    not native_executable().is_file(), reason="需要已构建的原生 sandbox",
)]


def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    home = RedPandaHome(tmp_path / "home")
    workspace = workspace_record(root)
    return root, home, workspace, ChildFiles(home, workspace)


async def edit(view, command, change):
    async def apply():
        change(view.native_path(view.root))
        return {"ok": True}
    return await view.execute(operation_id("child", command), apply)


@pytest.mark.parametrize("untouched", [0, 200])
def test_handoff_hashes_only_changed_files_and_reuses_frozen_result(tmp_path, untouched):
    async def scenario():
        root, home, workspace, files = setup(tmp_path)
        (root / "src").mkdir()
        (root / "src" / "code").write_text("base\n")
        for index in range(untouched):
            (root / f"untouched-{index}").write_bytes(b"U" * 8192)
        child = await files.create("parent", "child")
        view = workspace_files(home, child_workspace(workspace, child))
        await edit(view, "write", lambda mount: (mount / "src" / "code").write_text("child\n"))
        hashes = []
        original = ReviewWorktrees._hash
        def measured(backend, index, included):
            hashes.extend(path.decode() for path in included)
            return original(backend, index, included)
        with patch.object(ReviewWorktrees, "_hash", measured):
            await files.finish("parent", "child")
            assert hashes == ["src/code"]
            (child / "src" / "code").write_text("after handoff\n")
            await files.finish("parent", "child")
            compared = await files.compare("parent", "child", ["src/code"])
            assert "+child" in compared["diff"] and "after handoff" not in compared["diff"]
            assert hashes == ["src/code"]
            (root / "human").write_text("human")
            parent = workspace_files(home, workspace)
            async def merge():
                assert await files.merge("parent", "child", parent) == ()
                return {"ok": True}
            await parent.execute(operation_id("parent", "merge"), merge)
            assert hashes == ["src/code", "src/code"]
        assert (root / "src" / "code").read_text() == "child\n"
        assert (root / "human").read_text() == "human"
        await parent.restore(INITIAL, identity=operation_id("parent", "undo"), policy="original")
        assert (root / "src" / "code").read_text() == "base\n"
        assert (root / "human").read_text() == "human"
    asyncio.run(scenario())


def test_siblings_can_write_and_finish_without_waiting_for_each_other(tmp_path):
    async def scenario():
        root, home, workspace, files = setup(tmp_path)
        (root / "file").write_text("base")
        children = await asyncio.gather(*(files.create("parent", name) for name in ("a", "b")))
        views = [workspace_files(home, child_workspace(workspace, root)) for root in children]
        started = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()
        async def write(index):
            async def apply():
                started[index].set()
                await release.wait()
                views[index].native_path(views[index].root / "file").write_text(str(index))
                return {"ok": True}
            return await views[index].execute(operation_id(str(index), "write"), apply)
        writers = [asyncio.create_task(write(index)) for index in range(2)]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 20)
            # A's handoff waits for A, while B is still able to complete its write.
            finish = asyncio.create_task(files.finish("parent", "a"))
            await asyncio.sleep(0)
            assert not finish.done()
        finally:
            release.set()
            await asyncio.gather(*writers)
        await asyncio.wait_for(finish, 20)
        # Force simultaneous Git object writes in sibling handoffs.
        # A fresh pair avoids the already frozen A result above.
        for name in ("c", "d"):
            child = await files.create("parent", name)
            child_view = workspace_files(home, child_workspace(workspace, child))
            await edit(child_view, name, lambda mount: (mount / "file").write_text("changed"))
        barrier = threading.Barrier(2, timeout=15)
        original = ReviewWorktrees._hash
        def overlap(backend, index, included):
            barrier.wait()
            return original(backend, index, included)
        with patch.object(ReviewWorktrees, "_hash", overlap):
            await asyncio.wait_for(asyncio.gather(*(files.finish("parent", name) for name in ("c", "d"))), 20)
        await files.finish("parent", "b")
        assert (root / "file").read_text() == "base"
        assert (await files.compare("parent", "a"))["files"] == [{"status": "M", "path": "file"}]
        assert (await files.compare("parent", "b"))["files"] == [{"status": "M", "path": "file"}]
    asyncio.run(scenario())


def test_restored_and_ignored_changes_do_not_enter_result(tmp_path):
    async def scenario():
        root, home, workspace, files = setup(tmp_path)
        (root / "file").write_text("base")
        (root / ".gitignore").write_text("ignored\n")
        child = await files.create("parent", "child")
        view = workspace_files(home, child_workspace(workspace, child))
        await edit(view, "temporary", lambda mount: (mount / "file").write_text("temporary"))
        await view.restore(INITIAL, identity=operation_id("child", "undo"), policy="original")
        await edit(view, "ignored", lambda mount: (mount / "ignored").write_text("ignored"))
        await files.finish("parent", "child")
        assert await files.compare("parent", "child") == {"files": []}
    asyncio.run(scenario())


@pytest.mark.parametrize("directory", [False, True])
def test_file_directory_replacement_is_complete(tmp_path, directory):
    async def scenario():
        root, home, workspace, files = setup(tmp_path)
        if directory:
            (root / "node").mkdir()
            (root / "node" / "old").write_text("old")
        else:
            (root / "node").write_text("old")
        child = await files.create("parent", "child")
        view = workspace_files(home, child_workspace(workspace, child))
        def replace(mount):
            node = mount / "node"
            if directory:
                (node / "old").unlink()
                node.rmdir()
                node.write_text("new")
            else:
                node.unlink()
                node.mkdir()
                (node / "new").write_text("new")
        await edit(view, "replace", replace)
        await files.finish("parent", "child")
        parent = workspace_files(home, workspace)
        async def merge():
            assert await files.merge("parent", "child", parent) == ()
            return {"ok": True}
        await parent.execute(operation_id("parent", "merge"), merge)
        assert ((root / "node") if directory else (root / "node" / "new")).read_text() == "new"
        if not directory:
            (root / "node" / "human").write_text("human")
            with pytest.raises(WorkspaceRestoreFailed, match="directory_contains_user_children"):
                await parent.restore(INITIAL, identity=operation_id("parent", "blocked"), policy="original")
            assert (root / "node" / "human").read_text() == "human"
            assert (root / "node" / "new").read_text() == "new"
            (root / "node" / "human").unlink()
        await parent.restore(INITIAL, identity=operation_id("parent", "undo"), policy="original")
        assert ((root / "node" / "old") if directory else (root / "node")).read_text() == "old"
    asyncio.run(scenario())


def test_conflict_result_can_seed_an_independent_resolution_child(tmp_path):
    async def scenario():
        root, home, workspace, files = setup(tmp_path)
        (root / "file").write_text("base\n")
        child = await files.create("parent", "child")
        child_view = workspace_files(home, child_workspace(workspace, child))
        await edit(child_view, "edit", lambda mount: (mount / "file").write_text("child\n"))
        await files.finish("parent", "child")
        (root / "file").write_text("parent\n")
        parent = workspace_files(home, workspace)
        async def merge():
            assert await files.merge("parent", "child", parent) == ("file",)
            return {"ok": False}
        await parent.execute(operation_id("parent", "conflict"), merge)
        assert (root / "file").read_text() == "parent\n"
        resolution = await files.create("parent", "resolve", conflict_child_id="child")
        assert "<<<<<<<" in (resolution / "file").read_text()
        resolution_view = workspace_files(home, child_workspace(workspace, resolution))
        await edit(resolution_view, "resolve", lambda mount: (mount / "file").write_text("resolved\n"))
        await files.finish("parent", "resolve")
        async def accept():
            assert await files.merge("parent", "resolve", parent) == ()
            return {"ok": True}
        await parent.execute(operation_id("parent", "accept"), accept)
        assert (root / "file").read_text() == "resolved\n"
    asyncio.run(scenario())


def test_handoff_publishes_accepted_effects_after_interrupted_worker(tmp_path):
    async def scenario():
        root, home, workspace, files = setup(tmp_path)
        (root / "file").write_text("base")
        child = await files.create("parent", "child")
        view = workspace_files(home, child_workspace(workspace, child))
        identity = operation_id("child", "interrupted")
        from pathlib import Path
        with view._client() as client:
            begin = client.begin(identity)
            (Path(begin["mount"]) / "file").write_text("accepted")
            assert client.request("finish")["status"] == "sealed"
            assert client.request("accept", command_id=identity)["status"] == "accepted"
        assert (child / "file").read_text() == "base"
        await files.finish("parent", "child")
        assert (child / "file").read_text() == "accepted"
        assert (await files.compare("parent", "child"))["files"] == [{"status": "M", "path": "file"}]
    asyncio.run(scenario())
