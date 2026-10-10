import asyncio
import threading

import pytest

from redpanda.paths import RedPandaHome
from redpanda.sandbox.files.children import ChildFiles, child_root
from redpanda.sandbox.files.lifecycle import settled
from tests.fixtures.workspaces import workspace_record


def test_discard_only_removes_the_named_parents_children(tmp_path):
    home = RedPandaHome(tmp_path / "home")
    files = ChildFiles(home, workspace_record(tmp_path))
    own = child_root(home, "one", "child")
    other = child_root(home, "two", "child")
    for root in (own, other):
        root.mkdir(parents=True)
        (root / "file").write_text("keep")
    asyncio.run(files.discard("one", ["child"]))
    assert not own.exists()
    assert (other / "file").read_text() == "keep"


def test_repeated_cancellation_waits_for_file_side_effects_to_settle():
    async def scenario():
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        def operation():
            started.set()
            assert release.wait(10)
            finished.set()
        task = asyncio.create_task(settled(operation))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            assert not finished.is_set()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished.is_set()
    asyncio.run(scenario())
