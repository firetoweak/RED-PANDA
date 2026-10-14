"""平台中立的任务写入约束。

deny_root 是宿主任务根。可能写入的命令从挂载视图启动，不从宿主任务根启动。
本对象只描述边界；后端负责执行。约束接不上时后端必须拒绝启动。
约束状态不是进程退出码。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ConstraintFailure(Enum):
    """约束没能维持时的可观察类别，与进程退出码无关。"""

    UNSUPPORTED_PROCESS = "unsupported_process"
    NOT_HELD = "not_held"


@dataclass(frozen=True)
class TaskWriteConstraint:
    """一次任务的宿主拒写根。"""

    deny_root: Path

    def __post_init__(self) -> None:
        root = self.deny_root
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("deny_root 必须是绝对路径")
        object.__setattr__(self, "deny_root", root)

    def for_command(self, cwd: Path, *, may_write: bool) -> CommandConstraint:
        if not isinstance(cwd, Path) or not cwd.is_absolute():
            raise ValueError("命令启动目录必须是绝对路径")
        if may_write and cwd.is_relative_to(self.deny_root):
            raise RuntimeError(
                "可能写入的命令必须从任务挂载目录启动，不能从宿主任务根启动"
            )
        return CommandConstraint(
            deny_root=self.deny_root,
            cwd=cwd,
            may_write=may_write,
        )


@dataclass(frozen=True)
class CommandConstraint:
    """一条命令的写入边界。may_write 时 cwd 是挂载视图。"""

    deny_root: Path
    cwd: Path
    may_write: bool


def command_directory(location: Path | CommandConstraint) -> Path:
    if isinstance(location, CommandConstraint):
        return location.cwd
    if isinstance(location, Path):
        return location
    raise TypeError("命令位置必须是路径或 CommandConstraint")
