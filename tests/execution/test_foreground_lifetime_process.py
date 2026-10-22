"""Host shutdown must stop command writers and must not repeat unknown work."""
import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.process, pytest.mark.skipif(sys.platform not in {"win32", "linux"},
                                                     reason="Windows or Linux command ownership")]


def shell(*args):
    if os.name != "nt":
        return shlex.join(str(arg) for arg in args)
    return "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in args)


def until(predicate):
    deadline = time.monotonic() + 30
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("command lifecycle condition did not become true")
        time.sleep(.03)


def gone(pid):
    if os.name != "nt":
        return not Path("/proc", str(pid)).exists()
    from redpanda.execution.local.windows_job import SYNCHRONIZE, _kernel32
    process = _kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not process:
        return True
    try:
        return _kernel32.WaitForSingleObject(process, 0) == 0
    finally:
        _kernel32.CloseHandle(process)


@pytest.mark.parametrize("shutdown", ["close", "kill"])
@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
def test_host_exit_stops_writers_and_two_restarts_do_not_repeat_command(tmp_path, shutdown, background):
    root = Path(__file__).resolve().parents[2]
    if background:
        (tmp_path / "background-mode").touch()
    writer = (
        "import os,time;from pathlib import Path;"
        "Path('writer-pid').write_text(str(os.getpid()));"
        "\nwhile True:\n with Path('writes').open('a') as f: f.write('write\\n')\n time.sleep(.05)"
    )
    code = (
        "import os,subprocess,sys,time;from pathlib import Path;"
        "Path('command-pid').write_text(str(os.getpid()));"
        "\nwith Path('runs').open('a') as f: f.write('run\\n')\n"
        f"subprocess.Popen([sys.executable,'-c',{writer!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        + ("creationflags=subprocess.CREATE_NEW_PROCESS_GROUP" if os.name == "nt" else "start_new_session=True")
        + ");time.sleep(120)"
    )
    (tmp_path / "command").write_text(shell(sys.executable, "-c", code), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(root), REDPANDA_HOME=str(tmp_path / "home"))

    def start():
        (tmp_path / "ready").unlink(missing_ok=True)
        (tmp_path / "close-host").unlink(missing_ok=True)
        return subprocess.Popen([sys.executable, "-m", "tests.fixtures.foreground_host", str(tmp_path)],
                                cwd=root, env=env, stdout=log, stderr=log)

    def stop(host, mode):
        if mode == "kill":
            host.kill()
        else:
            (tmp_path / "close-host").touch()
        host.wait(timeout=30)
        if mode == "close":
            assert host.returncode == 0, (tmp_path / "host.log").read_text(errors="replace")

    with (tmp_path / "host.log").open("wb") as log:
        host = start()
        try:
            until(lambda: (tmp_path / "writes").exists() and (tmp_path / "ready").exists())
            if background:
                until(lambda: (tmp_path / "handoff").exists())
            pids = [int((tmp_path / name).read_text()) for name in ("command-pid", "writer-pid")]
            stop(host, shutdown)
            until(lambda: all(gone(pid) for pid in pids))
            writes = (tmp_path / "writes").read_bytes()
            for _ in range(2):
                host = start()
                until(lambda: (tmp_path / "ready").exists())
                assert json.loads((tmp_path / "ready").read_text())["execution_stopped"] is (not background)
                if background:
                    until(lambda: len((tmp_path / "model-calls").read_text().splitlines()) == 3)
                time.sleep(.2)
                assert (tmp_path / "model-calls").read_text().splitlines() == ["call"] * (3 if background else 1)
                assert (tmp_path / "runs").read_text().splitlines() == ["run"]
                assert (tmp_path / "writes").read_bytes() == writes
                stop(host, "close")
            if background:
                from redpanda.assistant.host.session_store import SessionStore
                from redpanda.runtime import DomainFactCommitted, SqliteJournal
                from redpanda.paths import RedPandaHome
                store = SessionStore(RedPandaHome(tmp_path / "home").runtime_sessions_root)
                events = asyncio.run(SqliteJournal(store.require("session")).snapshot("session"))
                facts = [event for event in events if isinstance(event.payload, DomainFactCommitted)
                         and event.payload.fact_type == "background_command_finished"]
                assert len(facts) == 1
                assert facts[0].payload.data["state"] == ("stopped" if shutdown == "close" else "unknown")
        finally:
            if host.poll() is None:
                host.kill()
                host.wait(timeout=30)
            # A failed regression must not leave its deliberate writer running.
            for name in ("command-pid", "writer-pid"):
                marker = tmp_path / name
                if marker.exists():
                    pid = int(marker.read_text())
                    if not gone(pid):
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
