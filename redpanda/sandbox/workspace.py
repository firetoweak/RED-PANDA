from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redpanda.sandbox.api import EnvironmentBinding


class EnvironmentInputError(ValueError):
    code = "ENVIRONMENT_INPUT_ERROR"


class InvalidEnvironmentPath(EnvironmentInputError):
    code = "INVALID_ENVIRONMENT_PATH"


class PathOutsideWorkspaceView(EnvironmentInputError):
    code = "PATH_OUTSIDE_WORKSPACE_VIEW"

    def __init__(self, path: str) -> None:
        super().__init__(f"path 不在工作区内: {path}")


class EnvironmentPermissionDenied(EnvironmentInputError):
    code = "ENVIRONMENT_PERMISSION_DENIED"

    def __init__(self, path: str, access: str) -> None:
        super().__init__(f"没有对该 path 的 {access} 权限: {path}")


class UnknownWorkspaceRoot(EnvironmentInputError):
    code = "UNKNOWN_WORKSPACE_ROOT"

    def __init__(self, root_id: str) -> None:
        super().__init__(f"未知的 workspace root: {root_id}")


class WorkspaceScope(str, Enum):
    TASK = "task_workspace"
    HOST = "host_filesystem"
    MATERIALS = "session_materials"


class FilesystemPermission(str, Enum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


@dataclass(frozen=True)
class RootBinding:
    root_id: str
    scope: WorkspaceScope
    path: Path

    def __post_init__(self) -> None:
        if not self.root_id or not self.root_id.strip():
            raise ValueError("workspace root id 不能为空")
        resolved = self.path.resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace root 不是已存在的目录: {resolved}")
        object.__setattr__(self, "path", resolved)

    def to_dict(self) -> dict[str, str]:
        return {
            "root_id": self.root_id,
            "scope": self.scope.value,
            "path": str(self.path),
        }

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> "RootBinding":
        if set(value) != {"root_id", "scope", "path"} or any(
            type(value[field]) is not str
            for field in ("root_id", "scope", "path")
        ):
            raise ValueError("workspace root 格式无效")
        return cls(
            root_id=value["root_id"],
            scope=WorkspaceScope(value["scope"]),
            path=Path(value["path"]),
        )


@dataclass(frozen=True)
class WorkspaceMembership:
    root_id: str
    scope: WorkspaceScope
    display_path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "root_id": self.root_id,
            "scope": self.scope.value,
            "display_path": self.display_path,
        }


@dataclass(frozen=True)
class WorkspaceViewSnapshot:
    roots: tuple[RootBinding, ...]

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("Workspace View 至少需要一个 root")
        ids = [root.root_id for root in self.roots]
        if len(set(ids)) != len(ids):
            raise ValueError("Workspace View root id 不能重复")
        paths = [root.path for root in self.roots]
        if len(set(paths)) != len(paths):
            raise ValueError("同一物理路径不能重复注册为多个 workspace root")

    def get(self, root_id: str) -> RootBinding:
        try:
            return next(root for root in self.roots if root.root_id == root_id)
        except StopIteration:
            raise UnknownWorkspaceRoot(root_id) from None

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        return {"roots": [root.to_dict() for root in self.roots]}

    @classmethod
    def from_dict(
        cls,
        value: dict[str, list[dict[str, str]]],
    ) -> "WorkspaceViewSnapshot":
        if set(value) != {"roots"} or not isinstance(value["roots"], list):
            raise ValueError("Workspace View 格式无效")
        if any(not isinstance(root, dict) for root in value["roots"]):
            raise ValueError("Workspace View root 必须是 object")
        return cls(tuple(
            RootBinding.from_dict(root)
            for root in value["roots"]
        ))

    def membership(self, path: Path) -> WorkspaceMembership:
        resolved = path.resolve()
        candidates = [
            root for root in self.roots if resolved.is_relative_to(root.path)
        ]
        if not candidates:
            raise PathOutsideWorkspaceView(str(path))
        root = max(candidates, key=lambda item: len(item.path.parts))
        relative = resolved.relative_to(root.path)
        return WorkspaceMembership(
            root_id=root.root_id,
            scope=root.scope,
            display_path=relative.as_posix() or ".",
        )


@dataclass(frozen=True)
class PermissionBinding:
    filesystem: tuple[tuple[str, FilesystemPermission], ...]
    network_access: str = "restricted"

    @classmethod
    def read_write(
        cls,
        view: WorkspaceViewSnapshot,
        *,
        network_access: str = "restricted",
    ) -> "PermissionBinding":
        return cls(tuple(
            (root.root_id, FilesystemPermission.READ_WRITE)
            for root in view.roots
        ), network_access=network_access)

    def access_for(self, root_id: str) -> FilesystemPermission:
        try:
            return next(
                access for candidate, access in self.filesystem
                if candidate == root_id
            )
        except StopIteration:
            raise EnvironmentPermissionDenied(root_id, "access") from None


@dataclass(frozen=True)
class EnvironmentLocation:
    environment_id: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "environment_id": self.environment_id,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> "EnvironmentLocation":
        if set(value) != {"environment_id", "path"} or any(
            type(value[field]) is not str
            for field in ("environment_id", "path")
        ):
            raise ValueError("Environment location 格式无效")
        return cls(
            environment_id=value["environment_id"],
            path=value["path"],
        )


@dataclass(frozen=True)
class ResolvedEnvironmentPath:
    native_path: Path
    location: EnvironmentLocation
    workspace_membership: WorkspaceMembership

    def result_fields(self) -> dict[str, object]:
        return {
            "location": self.location.to_dict(),
            "workspace_membership": self.workspace_membership.to_dict(),
        }


class WorkspacePathResolver:
    def __init__(self, binding: EnvironmentBinding) -> None:
        self.binding = binding

    def resolve(
        self,
        raw_path: str,
        *,
        access: str = "read",
    ) -> ResolvedEnvironmentPath:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise InvalidEnvironmentPath("path 不能为空")
        if "\x00" in raw_path:
            raise InvalidEnvironmentPath("path 不能包含 NUL")
        candidate = Path(raw_path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute() or candidate.drive
            else (self.binding.cwd / candidate).resolve()
        )
        membership = self.binding.workspace_view.membership(resolved)
        permission = self.binding.permission_binding.access_for(
            membership.root_id
        )
        if access == "write" and permission is not FilesystemPermission.READ_WRITE:
            raise EnvironmentPermissionDenied(raw_path, access)
        return ResolvedEnvironmentPath(
            native_path=resolved,
            location=EnvironmentLocation(
                environment_id=(
                    self.binding.execution_attachment.environment_instance_id
                ),
                path=resolved.as_uri(),
            ),
            workspace_membership=membership,
        )
