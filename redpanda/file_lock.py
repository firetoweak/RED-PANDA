"""跨进程文件锁；持有进程死亡时由操作系统释放。"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


# Windows 字节锁会挡住别人读被锁的区间；锁在文件内容之外的远端字节上。
_LOCK_OFFSET = 1 << 30


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """阻塞直到独占 ``path`` 这把锁，退出上下文时释放。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        lock(fd)
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)


if os.name == "nt":
    import msvcrt

    def try_lock(fd: int) -> bool:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except PermissionError:
            return False
        return True

    def lock(fd: int) -> None:
        # msvcrt 的阻塞模式每秒才重试一次且十次后放弃；这里自己短间隔等待。
        while not try_lock(fd):
            time.sleep(0.01)

    def unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
