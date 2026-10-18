"""一个数据根同一时刻只有一个 Host。"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from redpanda.file_lock import try_lock, unlock
from redpanda.paths import RedPandaHome


class DataRootInUse(RuntimeError):
    """数据根已被另一个仍在运行的 Host 持有。"""


@contextmanager
def hold_data_root(home: RedPandaHome) -> Iterator[None]:
    """独占数据根直到退出上下文；持有进程死亡时由操作系统释放。"""

    home.root.mkdir(parents=True, exist_ok=True)
    path = home.root / "host.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not try_lock(fd):
            owner = path.read_text(encoding="utf-8").strip() or "（占用者正在启动）"
            raise DataRootInUse(f"数据根 {home.root} 已被另一个 RED PANDA 占用：\n{owner}")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, _owner().encode("utf-8"))
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)


def _owner() -> str:
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    command = " ".join(sys.orig_argv)
    return f"进程：{os.getpid()}\n启动时间：{started}\n命令：{command}\n"
