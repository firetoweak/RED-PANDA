"""Linux FUSE transactions through versions.execute."""
import asyncio
import subprocess
import sys

import pytest

from redpanda.sandbox.versions import (
    INITIAL,
    WorkspaceRestoreFailed,
    WorkspaceVersions,
    native_executable,
    operation_id,
)

pytestmark = [pytest.mark.process, pytest.mark.skipif(
    sys.platform != "linux" or not native_executable().is_file(),
    reason="需要 Linux 上已构建的 redpanda-sandbox 与可用的 FUSE",
)]


def backend(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return WorkspaceVersions(root, tmp_path / "store")


def test_execute_publishes_writes_edits_and_deletes(tmp_path):
    async def scenario():
        view = backend(tmp_path)
        edited = view.root / "edit.txt"
        edited.write_text("A-Z", encoding="utf-8")
        gone = view.root / "gone.txt"
        gone.write_text("delete-me", encoding="utf-8")

        async def change():
            view.native_path(edited).write_text("X-Z", encoding="utf-8")
            view.native_path(gone).unlink()
            view.native_path(view.root / "new.txt").write_text("created", encoding="utf-8")
            completed = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", "from pathlib import Path; Path('from-command.txt').write_text('command')"],
                cwd=view.native_path(view.root),
                capture_output=True,
                check=True,
            )
            return {"ok": True, "returncode": completed.returncode}

        assert await view.execute(operation_id("s", "edit"), change) == {"ok": True, "returncode": 0}
        assert edited.read_text(encoding="utf-8") == "X-Z"
        assert not gone.exists()
        assert (view.root / "new.txt").read_text(encoding="utf-8") == "created"
        assert (view.root / "from-command.txt").read_text(encoding="utf-8") == "command"

    asyncio.run(scenario())


def test_replaced_file_conflicts_on_restore(tmp_path):
    async def scenario():
        view = backend(tmp_path)
        edited = view.root / "edit.txt"
        edited.write_text("AAAA", encoding="utf-8")

        async def change():
            view.native_path(edited).write_text("BBBB", encoding="utf-8")
            return {"ok": True}

        await view.execute(operation_id("s", "edit"), change)
        edited.unlink()
        edited.write_text("replaced", encoding="utf-8")
        with pytest.raises(WorkspaceRestoreFailed, match=r"(identity_changed|length_changed): /edit.txt"):
            await view.restore(INITIAL, identity=operation_id("s", "restore"), policy="original")
        assert edited.read_text(encoding="utf-8") == "replaced"

    asyncio.run(scenario())


@pytest.mark.parametrize("policy,expected", [("original", "A-U"), ("preserve", "H-U")])
def test_restore_policies_after_publish(tmp_path, policy, expected):
    async def scenario():
        view = backend(tmp_path)
        edited = view.root / "edit.txt"
        edited.write_text("A-Z", encoding="utf-8")
        (view.root / "gone.txt").write_text("delete-me", encoding="utf-8")

        async def change():
            view.native_path(edited).write_text("X-Z", encoding="utf-8")
            view.native_path(view.root / "gone.txt").unlink()
            view.native_path(view.root / "new.txt").write_text("created", encoding="utf-8")
            return {"ok": True}

        await view.execute(operation_id("s", "edit"), change)
        edited.write_text("H-U", encoding="utf-8")
        (view.root / "user.txt").write_text("human", encoding="utf-8")
        await view.restore(INITIAL, identity=operation_id("s", "restore"), policy=policy)
        assert edited.read_text(encoding="utf-8") == expected
        assert (view.root / "gone.txt").read_text(encoding="utf-8") == "delete-me"
        assert not (view.root / "new.txt").exists()
        assert (view.root / "user.txt").read_text(encoding="utf-8") == "human"

    asyncio.run(scenario())
