"""Windows task bootstrap protocol, separate from command output and exit codes."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import struct
import subprocess

from redpanda.execution.command import CommandConstraintError, CommandResult, CommandStartError
from redpanda.execution.constraint import ConstraintFailure

# 启动器状态协议里的阶段。共享错误类型不认识这些值。
_STAGE_CHILD = 3
_STAGE_RESOLVE = 4
_STAGE_REAP = 5
_STAGE_PROTOCOL = 6
_ERROR_NOT_SUPPORTED = 50


def constraint_failure(stage: int, error: int) -> ConstraintFailure:
    """把 Windows 状态协议映射成平台中立的约束失败。"""
    if stage == _STAGE_CHILD and error == _ERROR_NOT_SUPPORTED:
        return ConstraintFailure.UNSUPPORTED_PROCESS
    return ConstraintFailure.NOT_HELD


_STATUS = struct.Struct("<IIIII")


def native_task_launcher() -> Path:
    return Path(__file__).resolve().parents[1] / "bin" / "redpanda-command.exe"


class WindowsTask:
    def __init__(self, root: Path, cwd: Path, argv: list[str]) -> None:
        launcher = native_task_launcher()
        if not launcher.is_file() or not launcher.with_name("redpanda-hook.dll").is_file():
            raise CommandStartError("Windows task execution components are missing")
        self.read_fd, self.write_fd = os.pipe()
        os.set_inheritable(self.write_fd, True)
        import msvcrt
        handle = msvcrt.get_osfhandle(self.write_fd)
        self.startupinfo = subprocess.STARTUPINFO()
        self.startupinfo.lpAttributeList = {"handle_list": [handle]}
        self.argv = [str(launcher), str(handle), str(root), str(cwd), *argv]

    def spawned(self) -> None:
        os.close(self.write_fd)
        self.write_fd = None

    def close(self) -> None:
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None
        if self.read_fd is not None:
            os.close(self.read_fd)
            self.read_fd = None

    def finish(self, result: CommandResult) -> CommandResult:
        data = bytearray()
        try:
            while chunk := os.read(self.read_fd, 4096):
                data.extend(chunk)
        finally:
            self.close()
        if len(data) % _STATUS.size:
            raise RuntimeError("Truncated Windows task status")
        records = [_STATUS.unpack_from(data, offset) for offset in range(0, len(data), _STATUS.size)]
        if any(version != 1 for version, *_ in records):
            raise RuntimeError("Unsupported Windows task status version")
        if result.timed_out or result.interrupted:
            return result
        started = bool(records and records[0] == (1, 1, 0, 0, 0))
        terminal = records[1:] if started else records
        if len(terminal) != 1:
            raise RuntimeError(f"Windows task exited without terminal status: {records!r}")
        _, kind, stage, error, code = terminal[0]
        if kind == 3:
            if stage not in range(1, 7) or not error:
                raise RuntimeError(f"Invalid Windows task failure status: {terminal[0]!r}")
            if stage in (_STAGE_REAP, _STAGE_PROTOCOL):
                raise RuntimeError(f"Windows task lifecycle failure: stage={stage}, error={error}")
            if not started:
                raise CommandStartError(f"Windows task start failed: stage={stage}, {ctypes.WinError(error)}")
            if stage not in (_STAGE_CHILD, _STAGE_RESOLVE):
                raise RuntimeError(f"Invalid Windows task failure after start: {terminal[0]!r}")
            raise CommandConstraintError(result, constraint_failure(stage, error))
        if not started or kind != 2 or error or result.exit_code != 0:
            raise RuntimeError(f"Invalid Windows task terminal status: {terminal[0]!r}")
        from dataclasses import replace
        return replace(result, exit_code=code)
