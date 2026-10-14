"""Linux edges: symlinks, mode, publish races, stale mounts, escaped children."""
import errno
import os
from pathlib import Path
import signal
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

if sys.platform != "linux":
    pytest.skip("需要 Linux 与可用的 FUSE", allow_module_level=True)

from redpanda.paths import RedPandaHome
from redpanda.filesystem import clear_stale_mounts, workspace_files
from tests.fixtures.file_view import CommandClient
from redpanda.filesystem.file_view import publication as pub
from redpanda.filesystem.file_view.native_posix import identity, open_file
from redpanda.filesystem.operations import (
    INITIAL,
    WorkspaceFiles,
    native_executable,
    operation_id,
)

pytestmark = [pytest.mark.process, pytest.mark.skipif(
    sys.platform != "linux" or not native_executable().is_file(),
    reason="需要 Linux 上已构建的 redpanda-agentfs 与可用的 FUSE",
)]


def backend(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return WorkspaceFiles(root, tmp_path / "store")


def test_birth_time_survives_rewrite_and_changes_when_recreated(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("first", encoding="utf-8")
    with open_file(path) as handle:
        first = identity(handle)
    path.write_text("second", encoding="utf-8")
    with open_file(path) as handle:
        assert identity(handle) == first
    # overlay 的出生时间粒度较粗，同一时刻删了再建成可能仍是旧值。
    path.unlink()
    deadline = time.monotonic() + 2
    second = first
    while second == first:
        path.write_text("third", encoding="utf-8")
        with open_file(path) as handle:
            second = identity(handle)
        if second != first:
            break
        path.unlink()
        if time.monotonic() > deadline:
            raise AssertionError(first)
        time.sleep(0.02)
    assert second != first
    dev, ino, birth = first.split(":")[1:]
    assert len(dev) == len(ino) == len(birth) == 16


def test_symlink_and_executable_bit_publish_and_restore(tmp_path):
    import asyncio

    async def scenario():
        view = backend(tmp_path)
        script = view.root / "run.sh"
        script.write_text("echo\n", encoding="utf-8")
        script.chmod(0o644)

        async def change():
            root = view.native_path(view.root)
            (root / "pkg").mkdir()
            (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
            (root / "bin").mkdir()
            os.symlink("../pkg/mod.py", root / "bin" / "python")
            view.native_path(script).chmod(0o755)
            return {"ok": True}

        assert await view.execute(operation_id("s", "edit"), change) == {"ok": True}
        link = view.root / "bin" / "python"
        assert link.is_symlink()
        assert os.readlink(link) == "../pkg/mod.py"
        assert (view.root / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
        assert stat.S_IMODE(script.stat().st_mode) == 0o755
        await view.restore(INITIAL, identity=operation_id("s", "restore"), policy="original")
        assert not link.exists()
        assert not (view.root / "pkg").exists()
        assert stat.S_IMODE(script.stat().st_mode) == 0o644

    asyncio.run(scenario())


def test_rename_during_publish_keeps_the_user_file(tmp_path):
    import asyncio

    async def scenario():
        view = backend(tmp_path)
        edited = view.root / "edit.txt"
        edited.write_text("AAAA", encoding="utf-8")
        real = pub.native.open_file
        replaced = {"done": False}

        def wrapped(path, **kwargs):
            opened = real(path, **kwargs)
            if kwargs.get("write") and Path(path).name == "edit.txt":
                original = opened.write

                def write(data):
                    if not replaced["done"]:
                        replaced["done"] = True
                        temporary = Path(opened.name).with_name("user-save")
                        temporary.write_bytes(b"USER-SAVE")
                        os.replace(temporary, opened.name)
                    return original(data)

                opened.write = write
            return opened

        async def change():
            view.native_path(edited).write_text("BBBB", encoding="utf-8")
            return {"ok": True}

        with patch.object(pub.native, "open_file", wrapped):
            with pytest.raises(RuntimeError, match="identity changed during write"):
                await view.execute(operation_id("s", "edit"), change)
        assert edited.read_bytes() == b"USER-SAVE"

    asyncio.run(scenario())


def test_fifo_rejection_leaves_the_service_usable(tmp_path):
    root = tmp_path / "base"
    root.mkdir()
    (root / "keep.txt").write_text("A", encoding="utf-8")
    client = CommandClient(native_executable(), tmp_path / "store", root)
    try:
        begin = client.begin("c1")
        with pytest.raises(OSError) as caught:
            os.mkfifo(Path(begin["mount"]) / "pipe")
        assert caught.value.errno == errno.EOPNOTSUPP, caught.value
        sealed = client.request("finish")
        assert sealed["status"] == "sealed", sealed
        assert not any(change["path"] == "/pipe" for change in sealed["receipt"]["changes"])
        client.request("discard", command_id="c1")
        again = client.begin("c2")
        assert again["status"] == "active", again
        assert (Path(again["mount"]) / "keep.txt").read_text(encoding="utf-8") == "A"
        client.request("finish")
        client.request("discard", command_id="c2")
    finally:
        client.close()


def test_sigkill_mount_is_cleared_on_restart_and_the_view_resumes(tmp_path):
    home = RedPandaHome(tmp_path / "home")
    home.initialize()
    root = tmp_path / "base"
    root.mkdir()
    (root / "keep.txt").write_text("A", encoding="utf-8")
    view = workspace_files(home, SimpleNamespace(task_root=root))
    client = CommandClient(native_executable(), view.storage, root)
    begin = client.begin("c1")
    assert begin["status"] == "active"
    os.kill(client.process.pid, signal.SIGKILL)
    client.process.wait(timeout=20)
    client.close()
    assert begin["mount"] in Path("/proc/self/mounts").read_text(encoding="utf-8")

    clear_stale_mounts(home)

    assert begin["mount"] not in Path("/proc/self/mounts").read_text(encoding="utf-8")
    nxt = CommandClient(native_executable(), view.storage, root)
    try:
        assert nxt.request("status", command_id="c1")["status"] == "unknown"
        again = nxt.begin("c2")
        assert again["status"] == "active", again
        assert (Path(again["mount"]) / "keep.txt").read_text(encoding="utf-8") == "A"
        nxt.request("finish")
        nxt.request("discard", command_id="c2")
    finally:
        nxt.close()


def test_setsid_child_does_not_survive_the_command(tmp_path):
    root = tmp_path / "base"
    root.mkdir()
    store = tmp_path / "store"
    client = CommandClient(native_executable(), store, root)
    try:
        code = (
            "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"
            "\"import time;from pathlib import Path;time.sleep(1);Path('escaped').write_text('bad')\"],"
            "start_new_session=True);time.sleep(30)"
        )
        result = client.run("c1", [sys.executable, "-c", code], timeout=0.4)
        assert result["execution"]["timed_out"] is True
        time.sleep(1.2)
        changes = result["files"]["receipt"]["changes"]
        assert not any(change["path"] == "/escaped" for change in changes)
        assert not (root / "escaped").exists()
        client.request("discard", command_id="c1")
    finally:
        client.close()


def test_write_close_ignores_writeback_time_setattr(tmp_path):
    """Write and close through agentFS succeed while FUSE writeback stays on.

    Kernel writeback sends a timestamp SETATTR on close. Agent filesystem
    mounts ignore that update, so close does not return EIO and a touch-only
    file does not become history. Content changes still do.
    """
    root = tmp_path / "base"
    root.mkdir()
    (root / "keep.txt").write_text("A\n", encoding="utf-8")
    (root / "clock.txt").write_text("still\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["VFS_FUSE_WRITEBACK"] = "1"
    environment["VFS_FUSE_WORKERS"] = "2"
    client = CommandClient(
        native_executable(),
        tmp_path / "store",
        root,
        service_environment=environment,
    )
    try:
        begin = client.begin("c1")
        assert begin["status"] == "active", begin
        mount = Path(begin["mount"])
        (mount / "keep.txt").write_text("B\n", encoding="utf-8")
        (mount / "new.txt").write_text("fresh\n", encoding="utf-8")
        os.utime(mount / "clock.txt", (1_700_000_000, 1_700_000_000))
        os.utime(mount / "keep.txt", None)
        assert (mount / "keep.txt").read_text(encoding="utf-8") == "B\n"
        assert (mount / "new.txt").read_text(encoding="utf-8") == "fresh\n"
        assert (mount / "clock.txt").read_text(encoding="utf-8") == "still\n"
        sealed = client.request("finish")
        assert sealed["status"] == "sealed", sealed
        changes = {change["path"] for change in sealed["receipt"]["changes"]}
        assert "/clock.txt" not in changes
        assert {"/keep.txt", "/new.txt"} <= changes
    finally:
        client.close()
