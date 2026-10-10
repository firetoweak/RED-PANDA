import asyncio
from pathlib import Path
from unittest.mock import patch
import threading

import pytest

from redpanda.sandbox.files import read_text, replace_text, write_text


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
