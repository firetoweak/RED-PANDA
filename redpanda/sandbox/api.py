from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from redpanda.sandbox.command import EnvironmentCommandExecutor

from redpanda.sandbox.workspace import (
    EnvironmentInputError,
    PermissionBinding,
    WorkspacePathResolver,
    WorkspaceViewSnapshot,
)


class UnknownEnvironment(EnvironmentInputError):
    code = "UNKNOWN_ENVIRONMENT"

    def __init__(self, environment_id: str) -> None:
        super().__init__(f"未知的 Environment: {environment_id}")


@dataclass(frozen=True)
class EnvironmentSelection:
    environment_id: str
    workspace_view: WorkspaceViewSnapshot
    cwd: str

    def to_dict(self) -> dict[str, object]:
        return {
            "environment_id": self.environment_id,
            "workspace_view": self.workspace_view.to_dict(),
            "cwd": self.cwd,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "EnvironmentSelection":
        if set(value) != {"environment_id", "workspace_view", "cwd"}:
            raise ValueError("Environment selection 字段不匹配")
        workspace_view = value["workspace_view"]
        if not isinstance(workspace_view, dict):
            raise ValueError("workspace_view 必须是 object")
        environment_id = value["environment_id"]
        cwd = value["cwd"]
        if type(environment_id) is not str or type(cwd) is not str:
            raise ValueError("environment_id/cwd 必须是 string")
        return cls(
            environment_id=environment_id,
            workspace_view=WorkspaceViewSnapshot.from_dict(workspace_view),
            cwd=cwd,
        )


@dataclass(frozen=True)
class ExecutionAttachment:
    environment_instance_id: str
    command_executor: EnvironmentCommandExecutor
    process_sandbox: str = "unavailable"
    filesystem: object | None = None

    def __post_init__(self) -> None:
        if not self.environment_instance_id.strip():
            raise ValueError("environment instance id 不能为空")


@dataclass(frozen=True)
class EnvironmentBinding:
    environment_id: str
    workspace_view: WorkspaceViewSnapshot
    permission_binding: PermissionBinding
    cwd: Path
    shell_name: str
    shell_path: str
    execution_attachment: ExecutionAttachment

    def __post_init__(self) -> None:
        resolved_cwd = self.cwd.resolve()
        self.workspace_view.membership(resolved_cwd)
        object.__setattr__(self, "cwd", resolved_cwd)

    @property
    def resolver(self) -> WorkspacePathResolver:
        return WorkspacePathResolver(self)


class EnvironmentProvider(Protocol):
    async def attach(
        self,
        selection: EnvironmentSelection,
    ) -> EnvironmentBinding:
        ...


def environment_error(exc: EnvironmentInputError) -> dict[str, str | bool]:
    return {"ok": False, "code": exc.code, "error": str(exc)}


def file_error_message(exc: OSError) -> str:
    """Describe an I/O failure without exposing physical execution paths."""
    import errno
    if isinstance(exc, FileNotFoundError):
        return "文件或目录不存在，请确认路径后重试。"
    if isinstance(exc, PermissionError):
        return "文件无法访问，请检查访问权限或是否被其他程序占用。"
    if isinstance(exc, IsADirectoryError):
        return "目标是目录，请指定文件路径。"
    if isinstance(exc, NotADirectoryError):
        return "路径中的某一项不是目录，请检查路径。"
    if exc.errno == errno.ENOSPC:
        return "磁盘空间不足，请释放空间后重试。"
    if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
        return "此文件操作不受支持，请改用工具支持的文件操作。"
    return "文件操作未完成，请检查文件占用、访问权限和磁盘状态后重试。"
