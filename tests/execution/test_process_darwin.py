"""Exit races must not fail cleanup or hide a persistent signalling failure."""
import errno
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS process groups")


@pytest.fixture
def command(monkeypatch):
    from konden.execution import process_darwin as module

    running = object.__new__(module.Running)
    running.process = SimpleNamespace(pid=123, returncode=None, wait=Mock())
    running.logs = []
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds
        assert clock.now <= 11, "cleanup exceeded its bounded deadline"

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return module, running


@pytest.mark.parametrize("error", [
    PermissionError(errno.EPERM, "group is exiting"),
    ProcessLookupError(errno.ESRCH, "group is exiting"),
])
def test_cleanup_waits_for_group_to_drain_after_signal_error(command, monkeypatch, error):
    module, running = command
    monkeypatch.setattr(running, "_group_busy", lambda: module.time.monotonic() < .02)
    monkeypatch.setattr(module.os, "killpg", Mock(side_effect=error))

    running.close()

    running.process.wait.assert_called_once()


@pytest.mark.parametrize("error", [
    PermissionError(errno.EPERM, "permission denied"),
    ProcessLookupError(errno.ESRCH, "group unavailable"),
])
def test_cleanup_preserves_signal_error_when_group_cannot_drain(command, monkeypatch, error):
    module, running = command
    monkeypatch.setattr(running, "_group_busy", lambda: True)
    monkeypatch.setattr(module.os, "killpg", Mock(side_effect=error))

    with pytest.raises(type(error)) as caught:
        running.close()

    assert caught.value is error
    running.process.wait.assert_not_called()
