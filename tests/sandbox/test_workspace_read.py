import asyncio
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from redpanda.sandbox.versions import WorkspaceVersions


def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "file.txt").write_text("published")
    return WorkspaceVersions(root, tmp_path / "store")


def test_readers_overlap_across_workspace_owners(tmp_path):
    async def scenario():
        first = workspace(tmp_path)
        second = WorkspaceVersions(first.root, first.storage)
        entered = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def query(view, index):
            assert view.native_path(view.root / "file.txt").read_text() == "published"
            entered[index].set()
            await release.wait()

        tasks = [asyncio.create_task(view.read(lambda v=view, i=i: query(v, i)))
                 for i, view in enumerate((first, second))]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 5)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert not (first.storage / "commands").exists()
        assert not (first.storage / "HEAD").exists()
    asyncio.run(scenario())


def test_publication_waits_for_reader_in_another_owner(tmp_path):
    async def scenario():
        reader = workspace(tmp_path)
        publisher = WorkspaceVersions(reader.root, reader.storage)
        entered, release = asyncio.Event(), asyncio.Event()
        logical = reader.root / "file.txt"

        async def query():
            entered.set()
            await release.wait()
            assert reader.native_path(logical).read_text() == "published"

        task = asyncio.create_task(reader.read(query))
        await asyncio.wait_for(entered.wait(), 5)
        with patch.object(publisher, "_publish", lambda _: logical.write_text("next")):
            publishing = asyncio.create_task(publisher._publish_pending(None))
            try:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(publishing), 0.1)
                assert logical.read_text() == "published"
            finally:
                release.set()
                await asyncio.gather(task, publishing)
        assert logical.read_text() == "next"
    asyncio.run(scenario())


def test_cancelled_reader_releases_publication_lock(tmp_path):
    async def scenario():
        view = workspace(tmp_path)
        entered = asyncio.Event()

        async def query():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(view.read(query))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with patch.object(view, "_publish", lambda _: None):
            await asyncio.wait_for(view._publish_pending(None), 5)
        with pytest.raises(RuntimeError, match="outside a sandbox operation"):
            view.native_path(view.root)
    asyncio.run(scenario())


def test_publication_wait_cannot_exhaust_the_executor_needed_to_release_a_reader(tmp_path):
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        view = workspace(tmp_path)
        entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        connect = sqlite3.connect
        calls = 0
        def observe(*args, **kwargs):
            nonlocal calls
            # Bound the old blocking behavior so a regression fails promptly.
            kwargs["timeout"] = min(kwargs["timeout"], 0.2)
            calls += 1
            if calls == 2:
                loop.call_soon_threadsafe(attempted.set)
            return connect(*args, **kwargs)
        async def query():
            entered.set()
            await release.wait()
        with patch("redpanda.sandbox.versions.sqlite3.connect", observe):
            reading = asyncio.create_task(view.read(query))
            await asyncio.wait_for(entered.wait(), 5)
            with patch.object(view, "_publish", lambda _: None):
                publishing = asyncio.create_task(view._publish_pending(None))
                try:
                    await asyncio.wait_for(attempted.wait(), 5)
                finally:
                    release.set()
                await asyncio.wait_for(asyncio.gather(reading, publishing), 5)
    asyncio.run(scenario())


def test_cancelled_publication_wait_does_not_wait_for_the_reader(tmp_path, monkeypatch):
    connect = sqlite3.connect
    def bounded(*args, **kwargs):
        kwargs["timeout"] = min(kwargs["timeout"], 0.2)
        return connect(*args, **kwargs)
    monkeypatch.setattr("redpanda.sandbox.versions.sqlite3.connect", bounded)
    async def scenario():
        view = workspace(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        async def query():
            entered.set()
            await release.wait()
        reading = asyncio.create_task(view.read(query))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            with patch.object(view, "_publish", lambda _: pytest.fail("cancelled publication ran")):
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(view._publish_pending(None), 0.1)
            assert not reading.done()
        finally:
            release.set()
            await reading
        with patch.object(view, "_publish", lambda _: None):
            await asyncio.wait_for(view._publish_pending(None), 5)
    asyncio.run(scenario())


def test_operation_owner_can_wait_for_a_published_read(tmp_path):
    async def scenario():
        view = workspace(tmp_path)
        async def query():
            return view.native_path(view.root / "file.txt").read_text()
        async with view._owner():
            assert await asyncio.wait_for(view.read(query), 5) == "published"
    asyncio.run(scenario())


def test_waiting_publication_does_not_block_a_readers_dependent_query(tmp_path):
    async def scenario():
        view = workspace(tmp_path)
        other = WorkspaceVersions(view.root, view.storage)
        entered, dependent, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        connect = sqlite3.connect
        calls = 0
        def observe(*args, **kwargs):
            nonlocal calls
            kwargs["timeout"] = min(kwargs["timeout"], 0.2)
            calls += 1
            if calls == 2:
                loop.call_soon_threadsafe(attempted.set)
            return connect(*args, **kwargs)
        async def inner():
            return other.native_path(other.root / "file.txt").read_text()
        async def query():
            entered.set()
            await dependent.wait()
            return await other.read(inner)
        with patch("redpanda.sandbox.versions.sqlite3.connect", observe):
            reading = asyncio.create_task(view.read(query))
            await asyncio.wait_for(entered.wait(), 5)
            with patch.object(view, "_publish", lambda _: (view.root / "file.txt").write_text("next")):
                publishing = asyncio.create_task(view._publish_pending(None))
                try:
                    await asyncio.wait_for(attempted.wait(), 5)
                finally:
                    dependent.set()
                result, _ = await asyncio.wait_for(asyncio.gather(reading, publishing), 5)
        assert result == "published"
        assert (view.root / "file.txt").read_text() == "next"
    asyncio.run(scenario())


def test_lock_storage_failure_is_not_retried_as_contention(tmp_path):
    async def scenario():
        view = workspace(tmp_path)
        failure = sqlite3.OperationalError("disk I/O error")
        failure.sqlite_errorcode = sqlite3.SQLITE_IOERR
        with patch("redpanda.sandbox.versions.sqlite3.connect", side_effect=failure) as connect:
            with pytest.raises(sqlite3.OperationalError) as raised:
                await view.read(lambda: asyncio.sleep(0))
            assert raised.value is failure
            connect.assert_called_once()
    asyncio.run(scenario())


def test_read_does_not_hide_corrupt_publication_metadata(tmp_path):
    async def scenario():
        view = workspace(tmp_path)
        folder = view.storage / "host"
        folder.mkdir(parents=True)
        (folder / "broken.json").write_text("{")
        with pytest.raises(json.JSONDecodeError):
            await view.read(lambda: asyncio.sleep(0))
    asyncio.run(scenario())


def test_read_rejects_unfinished_publication(tmp_path):
    async def scenario():
        view = workspace(tmp_path)
        folder = view.storage / "host"
        folder.mkdir(parents=True)
        (folder / "pending.json").write_text(json.dumps({
            "version": 3, "kind": "publish", "commands": ["a" * 64],
            "commit": str(uuid.uuid4()), "steps": [],
            "state": "prepared", "finalized": False,
        }))
        with pytest.raises(RuntimeError, match="requires reconciliation"):
            await view.read(lambda: asyncio.sleep(0))
    asyncio.run(scenario())
