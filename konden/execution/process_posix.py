"""Managed raw Linux task process lifetime."""
from __future__ import annotations
import ctypes
import ctypes.util
import os
from pathlib import Path
import signal
import subprocess
import time

PR_SET_CHILD_SUBREAPER = 36
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
_libc.prctl.restype = ctypes.c_int

def _stat_fields(pid: int):
    try:
        text = Path("/proc", str(pid), "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    end = text.rfind(")")
    if end < 0:
        return None
    fields = text[end + 2 :].split()
    if len(fields) < 3:
        return None
    return fields


def _enable_subreaper():
    if _libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), "PR_SET_CHILD_SUBREAPER")


def _child_pids():
    found = []
    for thread in os.scandir("/proc/self/task"):
        try:
            children = Path(thread.path, "children").read_text(encoding="ascii")
        except FileNotFoundError:
            continue  # The thread exited while reading its children.
        found.extend(int(pid) for pid in children.split())
    return found


class Running:
    """Own the caller's children; persistent tasks isolate this in process_linux."""
    def __init__(self, argv, cwd, environment, log_dir):
        self.logs = []
        self.process = None
        _enable_subreaper()
        self._protected = set(_child_pids())
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
        # After the leader is reaped, killpg(pid) addresses that pgid number.
        # Skip it once the group is empty so a reused pid is not signaled.
        if self.process.returncode is not None and not self._group_busy():
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
            fields = _stat_fields(int(entry.name))
            if fields is not None and int(fields[2]) == pgid and fields[0] != "Z":
                return True
        return False

    def _leader_running(self) -> bool:
        # /proc stays until wait reaps the zombie, so killpg still names this pid.
        if self.process is None:
            return False
        fields = _stat_fields(self.process.pid)
        return fields is not None and fields[0] != "Z"

    def _escaped_children(self):
        if self.process is None:
            return []
        live = []
        for pid in _child_pids():
            if pid in self._protected or pid == self.process.pid:
                continue
            fields = _stat_fields(pid)
            if fields is not None and fields[0] != "Z":
                live.append(pid)
        return live

    def _kill_escaped(self):
        for pid in self._escaped_children():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _reap_zombies(self):
        if self.process is None:
            return
        for pid in _child_pids():
            if pid in self._protected or pid == self.process.pid:
                continue
            fields = _stat_fields(pid)
            if fields is None or fields[0] != "Z":
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass

    def _drain_descendants(self):
        # Kill while the leader pid is still allocated. Reaping first would
        # let that pid be reused before killpg. Escaped setsid children become
        # zombies of this subreaper and have to be waited on.
        deadline = time.monotonic() + 10
        while True:
            self._reap_zombies()
            if not self._group_busy() and not self._escaped_children():
                return
            self._signal_group()
            self._kill_escaped()
            if time.monotonic() > deadline:
                raise TimeoutError("process group did not drain")
            time.sleep(0.01)

    def wait(self, timeout=60, cancel_event=None):
        start = time.monotonic()
        timed_out = False
        interrupted = False
        deadline = start + timeout
        while self._leader_running():
            if cancel_event is not None and cancel_event.is_set():
                interrupted = True
                self._signal_group()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._signal_group()
                break
            time.sleep(0.02)
        self._drain_descendants()
        self.process.wait(timeout=10)
        code = self.process.returncode
        self.close()
        return {
            "exit_code": code,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "duration_ms": (time.monotonic() - start) * 1000,
        }

    @property
    def pid(self):
        return self.process.pid

    def poll(self, *, leader_only=False):
        self._reap_zombies()
        if self._leader_running():
            return None
        # Once the leader is a zombie, every remaining descendant has a direct
        # ancestor owned by this subreaper. Read the kernel's child list instead
        # of a /proc-wide census that can miss a newly reparented descendant.
        if not leader_only and any(pid != self.process.pid and pid not in self._protected for pid in _child_pids()):
            return None
        status = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if status is None:
            return None
        # Keep the leader allocated until close has drained the entire tree.
        return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status

    def close(self):
        if self.process is not None and self.process.returncode is None:
            if self._leader_running():
                self._signal_group()
            self._drain_descendants()
            self.process.wait(timeout=10)
        for stream in self.logs:
            if not stream.closed:
                stream.flush()
                if stream.writable():
                    os.fsync(stream.fileno())
                stream.close()
