import asyncio
from pathlib import Path
from unittest.mock import patch
import threading
import stat
from types import SimpleNamespace

import pytest

from konden.filesystem import read_text, replace_text, write_text
from konden.filesystem.workspace import (
    FileAccess, PermissionBinding, RootBinding, WorkspaceScope, WorkspaceViewSnapshot,
)
from konden.tools.builtin.file_manage import create_file_manage_specs
from konden.tools.builtin.file_write import create_file_write_specs


@pytest.mark.parametrize("source", [b"left\nTARGET\nright\n", b"left\r\nTARGET\r\nright\r\n",
                                  b"left\r\nTARGET\nright\r"])
def test_local_edit_preserves_all_unedited_bytes(tmp_path, source):
    path = tmp_path / "file"
    path.write_bytes(source)
    result = asyncio.run(replace_text(path, "TARGET", "中文", all_matches=False))
    assert result["ok"]
    assert path.read_bytes() == source.replace(b"TARGET", "中文".encode())


def test_read_and_edit_share_the_same_original_newlines(tmp_path):
    async def scenario():
        path = tmp_path / "file"
        await write_text(path, "first\r\nsecond\n", overwrite=False)
        result = await read_text(path, offset=1, limit=1, max_chars=100)
        assert result["content"] == "first\r\n"
        assert result["next_offset"] == 2
        assert (await replace_text(path, result["content"], "changed\r\n", all_matches=False))["ok"]
        assert path.read_bytes() == b"changed\r\nsecond\n"
    asyncio.run(scenario())


def test_duplicate_match_never_writes_without_an_explicit_replace_all(tmp_path):
    async def scenario():
        path = tmp_path / "file"
        source = b"TARGET\r\nTARGET\n"
        path.write_bytes(source)
        assert (await replace_text(path, "TARGET", "new", all_matches=False))["code"] == "OLD_BLOCK_NOT_UNIQUE"
        assert path.read_bytes() == source
        assert (await replace_text(path, "TARGET", "new", all_matches=True))["replacements"] == 2
        assert path.read_bytes() == b"new\r\nnew\n"
    asyncio.run(scenario())


def test_slow_file_read_does_not_block_other_async_work(tmp_path):
    async def scenario():
        path = tmp_path / "file"
        path.write_bytes(b"hello\n")
        entered, release = threading.Event(), threading.Event()
        original = Path.open
        def slow_open(target, *args, **kwargs):
            if target == path:
                entered.set()
                assert release.wait(5)
            return original(target, *args, **kwargs)
        with patch.object(Path, "open", slow_open):
            task = asyncio.create_task(read_text(path, offset=1, limit=1, max_chars=100))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert not task.done()
            finally:
                release.set()
                assert (await task)["content"] == "hello\n"
    asyncio.run(scenario())


@pytest.mark.parametrize("tool", ["write_file", "apply_patch", "replace_all"])
@pytest.mark.parametrize("privileged", [False, True])
def test_full_access_still_respects_a_file_made_read_only_before_approval(tmp_path, tool, privileged):
    async def scenario():
        path = tmp_path / "file"
        path.write_bytes(b"TARGET\n")
        mode = path.stat().st_mode
        view = WorkspaceViewSnapshot((RootBinding("host", WorkspaceScope.HOST, tmp_path),))
        binding = FileAccess("test", view, PermissionBinding.read_write(view), tmp_path)
        specs = create_file_manage_specs(binding) + create_file_write_specs(binding)
        spec = next(item for item in specs if item.name == tool)
        arguments = {"path": str(path), "content": "new", "overwrite": True} if tool == "write_file" else {
            "path": str(path), "old_block": "TARGET", "new_block": "new"}
        assert spec.requires_authorization(arguments) is True
        original_open = Path.open

        def privileged_open(target, mode="r", *args, **kwargs):
            # Model a privileged OS account that can write despite read-only mode bits.
            if privileged and target == path and any(flag in mode for flag in ("w", "a", "+")):
                target.chmod(target.stat().st_mode | stat.S_IWUSR)
            return original_open(target, mode, *args, **kwargs)

        try:
            path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            with patch.object(Path, "open", privileged_open):
                result = await spec.handler(SimpleNamespace(**arguments))
            assert not result["ok"]
            assert path.read_bytes() == b"TARGET\n"
        finally:
            path.chmod(mode)
    asyncio.run(scenario())
