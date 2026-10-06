from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import unittest

import pytest

pytestmark = pytest.mark.process


_LAUNCHER = """
from multiprocessing import get_context
from pathlib import Path
import sys

from redpanda.assistant.host.spawn import start_worker
from tests.fixtures.spawn_target import write_marker

marker = Path(sys.argv[1])
process = get_context("spawn").Process(
    target=write_marker,
    args=(str(marker),),
    name="spawn-probe",
)
start_worker(process)
process.join(20)
print(
    f"alive={process.is_alive()} exit={process.exitcode} marker={marker.is_file()}",
    flush=True,
)
if process.is_alive():
    process.terminate()
    process.join(5)
"""

_PATH_LAUNCHER = """
from multiprocessing import get_context
from pathlib import Path
import sys

from redpanda.assistant.host.spawn import start_worker
from tests.fixtures.spawn_target import write_path

marker = Path(sys.argv[1])
process = get_context("spawn").Process(
    target=write_path,
    args=(str(marker),),
    name="spawn-env-probe",
)
start_worker(process)
process.join(20)
print(
    f"alive={process.is_alive()} exit={process.exitcode} marker={marker.is_file()}",
    flush=True,
)
if process.is_alive():
    process.terminate()
    process.join(5)
"""


class WorkerSpawnTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "需要 Windows 句柄继承")
    def test_worker_does_not_keep_unrelated_inheritable_pipe_open(self) -> None:
        from redpanda.assistant.host.spawn import _windows_nul_stdio

        read_fd, write_fd = os.pipe()
        captured = []
        with os.fdopen(read_fd, "rb") as source, os.fdopen(write_fd, "wb") as destination:
            os.set_inheritable(write_fd, True)
            with _windows_nul_stdio():
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            reader = threading.Thread(target=lambda: captured.append(source.read()))
            try:
                destination.close()
                reader.start()
                reader.join(2)
                self.assertFalse(reader.is_alive(), "Worker 继承了其他子进程的管道写端，阻止 EOF")
                self.assertEqual(captured, [b""])
            finally:
                child.kill()
                child.wait(timeout=5)
                reader.join(5)

    @unittest.skipUnless(os.name == "nt", "需要 Windows 进程启动")
    def test_worker_stdio_redirect_does_not_capture_other_threads_subprocesses(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from redpanda.assistant.host.spawn import _windows_nul_stdio

        with ThreadPoolExecutor(max_workers=1) as pool:
            with _windows_nul_stdio():
                result = pool.submit(
                    subprocess.run,
                    [sys.executable, "-c", "print('other-thread-output')"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                ).result(timeout=15)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"other-thread-output")

    def test_spawn_reaches_target_when_host_stdio_is_piped(self) -> None:
        project = Path(__file__).resolve().parents[2]
        directory = tempfile.TemporaryDirectory()
        marker = Path(directory.name) / "marker.txt"
        env = os.environ.copy()
        pythonpath = str(project)
        if env.get("PYTHONPATH"):
            pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
        env["PYTHONPATH"] = pythonpath
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _LAUNCHER, str(marker)],
            cwd=str(project),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        collected: list[bytes] = []

        def read_stdout() -> None:
            assert proc.stdout is not None
            collected.append(proc.stdout.readline())

        reader = threading.Thread(target=read_stdout)
        reader.start()
        reader.join(25)
        try:
            if reader.is_alive():
                proc.kill()
                self.fail("worker spawn hung while parent stdio was piped")
            line = collected[0].decode("utf-8", errors="replace") if collected else ""
            self.assertIn("marker=True", line, msg=line)
            self.assertTrue(marker.is_file())
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            if proc.stderr is not None:
                proc.stderr.read()
            directory.cleanup()

    @unittest.skipUnless(os.name == "nt", "需要 Windows 用户会话环境")
    def test_worker_inherits_user_session_path_when_host_path_is_stripped(self) -> None:
        project = Path(__file__).resolve().parents[2]
        directory = tempfile.TemporaryDirectory()
        marker = Path(directory.name) / "path.txt"
        env = {
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            "SYSTEMDRIVE": os.environ.get("SYSTEMDRIVE", "C:"),
            "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT"),
            "TEMP": os.environ.get("TEMP", directory.name),
            "TMP": os.environ.get("TMP", directory.name),
            "USERPROFILE": os.environ.get("USERPROFILE", ""),
            "COMSPEC": os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "PYTHONPATH": str(project),
            "PATH": r"C:\Windows\system32",
        }
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _PATH_LAUNCHER, str(marker)],
            cwd=str(project),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        collected: list[bytes] = []

        def read_stdout() -> None:
            assert proc.stdout is not None
            collected.append(proc.stdout.readline())

        reader = threading.Thread(target=read_stdout)
        reader.start()
        reader.join(25)
        try:
            if reader.is_alive():
                proc.kill()
                self.fail("worker spawn hung while parent PATH was stripped")
            line = collected[0].decode("utf-8", errors="replace") if collected else ""
            self.assertIn("marker=True", line, msg=line)
            child_path = marker.read_text(encoding="utf-8")
            self.assertIsNotNone(shutil.which("powershell.exe", path=child_path))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            if proc.stderr is not None:
                proc.stderr.read()
            directory.cleanup()
