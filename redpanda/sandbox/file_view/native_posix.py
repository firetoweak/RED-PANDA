"""POSIX file identity and a process-group command lifetime."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time


def _format(st) -> str:
    # Same text as HostFS::file_identity on Linux.
    return f"unix:{st.st_dev:016x}:{st.st_ino:016x}"


def identity(file):
    return _format(os.fstat(file.fileno()))


def directory_identity(path):
    return _format(os.lstat(path))


class _Opened:
    def __init__(self, file, path):
        self._file = file
        self.name = os.fspath(path)

    def __getattr__(self, item):
        return getattr(self._file, item)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._file.close()
        return False


def open_file(path: Path, *, create=False, write=False):
    flags = os.O_RDWR if write else os.O_RDONLY
    flags |= os.O_CLOEXEC | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o666)
    return _Opened(os.fdopen(fd, "r+b" if write else "rb"), path)


def remove_open(file):
    path = file.name
    before = os.fstat(file.fileno())
    current = os.lstat(path)
    if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
        raise FileNotFoundError(path)
    os.unlink(path)


class Running:
    def __init__(self, argv, cwd, environment, log_dir):
        self.logs = []
        self.process = None
        log_dir = Path(log_dir)
        stdin = (log_dir / "stdin.bin").open("wb+")
        stdin.truncate(0)
        stdin.seek(0)
        stdout = (log_dir / "stdout.bin").open("wb")
        stderr = (log_dir / "stderr.bin").open("wb")
        self.logs = [stdin, stdout, stderr]
        try:
            self.process = subprocess.Popen(
                [str(item) for item in argv],
                cwd=os.fspath(cwd),
                env={str(key): str(value) for key, value in environment.items()},
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except BaseException:
            self.close()
            raise

    def _signal_group(self):
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _group_busy(self) -> bool:
        if self.process is None:
            return False
        pgid = self.process.pid
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                text = Path(entry.path, "stat").read_text(encoding="utf-8")
            except OSError:
                continue
            end = text.rfind(")")
            if end < 0:
                continue
            fields = text[end + 2 :].split()
            if len(fields) < 3:
                continue
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
        return False

    def _drain_group(self):
        self._signal_group()
        deadline = time.monotonic() + 10
        while self._group_busy():
            if time.monotonic() > deadline:
                raise TimeoutError("process group did not drain")
            time.sleep(0.01)

    def wait(self, timeout=60, cancel_event=None):
        start = time.monotonic()
        timed_out = False
        interrupted = False
        deadline = start + timeout
        while self.process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                interrupted = True
                self._signal_group()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._signal_group()
                break
            time.sleep(0.02)
        self.process.wait(timeout=10)
        self._drain_group()
        code = self.process.returncode
        self.close()
        return {
            "exit_code": code,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "duration_ms": (time.monotonic() - start) * 1000,
        }

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self._signal_group()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for stream in self.logs:
            if not stream.closed:
                stream.flush()
                if stream.writable():
                    os.fsync(stream.fileno())
                stream.close()
