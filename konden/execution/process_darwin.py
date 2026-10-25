"""Own a macOS process group; detached sessions are outside this boundary."""
from __future__ import annotations

import ctypes as c
import errno
import os
from pathlib import Path
import signal
import subprocess
import time


class BsdInfo(c.Structure):
    # Public proc_bsdinfo from <sys/proc_info.h> (PROC_PIDTBSDINFO).
    _fields_ = [(name, c.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved",
    )] + [("comm", c.c_char * 16), ("name", c.c_char * 32)] + [
        (name, c.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")
    ] + [("nice", c.c_int32), ("start_seconds", c.c_uint64), ("start_microseconds", c.c_uint64)]


_libproc = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
_libproc.proc_pidinfo.argtypes = [c.c_int, c.c_int, c.c_uint64, c.c_void_p, c.c_int]
_libproc.proc_pidinfo.restype = c.c_int
_libproc.proc_listpgrppids.argtypes = [c.c_int, c.c_void_p, c.c_int]
_libproc.proc_listpgrppids.restype = c.c_int
SZOMB = 5


def process_info(pid):
    info = BsdInfo()
    # A nonzero argument includes zombies, whose PID we retain until close.
    size = _libproc.proc_pidinfo(pid, 3, 1, c.byref(info), c.sizeof(info))
    if size == 0 and c.get_errno() == errno.ESRCH:
        return None
    if size != c.sizeof(info):
        error = c.get_errno()
        raise OSError(error, os.strerror(error), f"proc_pidinfo({pid})")
    return info


class Running:
    def __init__(self, argv, cwd, environment, log_dir):
        self.process = None
        self.logs = []
        root = Path(log_dir)
        try:
            for name in ("stdin", "stdout", "stderr"):
                self.logs.append((root / f"{name}.bin").open("wb+"))
            self.process = subprocess.Popen(
                argv, cwd=cwd, env=environment,
                stdin=self.logs[0], stdout=self.logs[1], stderr=self.logs[2],
                start_new_session=True,
            )
        except BaseException:
            self.close()
            raise

    @property
    def pid(self):
        return self.process.pid

    def _group_busy(self):
        capacity = _libproc.proc_listpgrppids(self.pid, None, 0)
        if capacity < 0:
            error = c.get_errno()
            raise OSError(error, os.strerror(error), "proc_listpgrppids")
        while True:
            pids = (c.c_int * (capacity + 16))()
            count = _libproc.proc_listpgrppids(self.pid, pids, c.sizeof(pids))
            if count < 0:
                error = c.get_errno()
                raise OSError(error, os.strerror(error), "proc_listpgrppids")
            if count < len(pids):
                break
            capacity = len(pids) * 2
        return any(info.status != SZOMB
                   for pid in pids[:count] if (info := process_info(pid)) is not None)

    def poll(self, *, leader_only=False):
        # Keep the leader as an unreaped zombie until the group has drained.
        # Its PID therefore cannot be reused before the final killpg.
        info = process_info(self.pid)
        if info is None:
            raise ProcessLookupError(self.pid)
        if info.status != SZOMB or (not leader_only and self._group_busy()):
            return None
        return os.waitstatus_to_exitcode(info.xstatus)

    def close(self):
        if self.process is not None and self.process.returncode is None:
            deadline = time.monotonic() + 10
            while self._group_busy():
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # Darwin returns EPERM when only zombies remain in the group.
                    if self._group_busy():
                        raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("macOS command process group did not drain")
                time.sleep(.01)
            self.process.wait(timeout=10)
        for stream in self.logs:
            if not stream.closed:
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
