"""POSIX file identity and exclusive host file operations."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path

AT_FDCWD = -100
AT_EMPTY_PATH = 0x1000
AT_SYMLINK_NOFOLLOW = 0x100
STATX_BTIME = 0x800


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


def set_path_mode(path, expected, desired, identity):
    fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if _format(info, _birth_ns(fd, b"", AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW)) != identity or info.st_mode & 0o7777 != expected:
            return False
        os.chmod(f"/proc/self/fd/{fd}", desired)
        return True
    finally:
        os.close(fd)


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


def open_file(path: Path, *, create=False, write=False, shared_read=False):
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
