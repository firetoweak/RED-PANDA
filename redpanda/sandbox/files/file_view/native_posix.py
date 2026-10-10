"""POSIX file identity and a process-group command lifetime."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path
import signal
import subprocess
import time

AT_FDCWD = -100
AT_EMPTY_PATH = 0x1000
AT_SYMLINK_NOFOLLOW = 0x100
STATX_BTIME = 0x800
PR_SET_CHILD_SUBREAPER = 36


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("__reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    # linux/stat.h struct statx, including the spare the kernel copies out.
    # A shorter buffer is overrun by statx and corrupts the heap.
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("__spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("stx_mnt_id", ctypes.c_uint64),
        ("stx_dio_mem_align", ctypes.c_uint32),
        ("stx_dio_offset_align", ctypes.c_uint32),
        ("stx_subvol", ctypes.c_uint64),
        ("stx_atomic_write_unit_min", ctypes.c_uint32),
        ("stx_atomic_write_unit_max", ctypes.c_uint32),
        ("stx_atomic_write_segments_max", ctypes.c_uint32),
        ("__spare1", ctypes.c_uint32),
        ("__spare3", ctypes.c_uint64 * 9),
    ]


_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.statx.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_uint,
    ctypes.POINTER(_Statx),
]
_libc.statx.restype = ctypes.c_int
_libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
_libc.prctl.restype = ctypes.c_int


def _birth_ns(dirfd, path, flags) -> int:
    buf = _Statx()
    if _libc.statx(dirfd, path, flags, STATX_BTIME, ctypes.byref(buf)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), "statx")
    if (buf.stx_mask & STATX_BTIME) == 0 or buf.stx_btime.tv_sec < 0:
        return 0
    return buf.stx_btime.tv_sec * 1_000_000_000 + buf.stx_btime.tv_nsec


def _format(st, birth: int) -> str:
    # Same text as HostFS::file_identity on Linux: dev, inode, statx birth time.
    return f"unix:{st.st_dev:016x}:{st.st_ino:016x}:{birth:016x}"


def identity(file):
    fd = file.fileno()
    return _format(os.fstat(fd), _birth_ns(fd, b"", AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW))


def directory_identity(path):
    return _format(
        os.lstat(path),
        _birth_ns(AT_FDCWD, os.fsencode(path), AT_SYMLINK_NOFOLLOW),
    )


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
    me = os.getpid()
    found = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        fields = _stat_fields(int(entry.name))
        if fields is not None and int(fields[1]) == me:
            found.append(int(entry.name))
    return found


class Running:
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

    def close(self):
        if self.process is not None and self.process.returncode is None:
            if self._leader_running():
                self._signal_group()
            try:
                self._drain_descendants()
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for stream in self.logs:
            if not stream.closed:
                stream.flush()
                if stream.writable():
                    os.fsync(stream.fileno())
                stream.close()
