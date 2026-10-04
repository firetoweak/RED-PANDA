"""工作区注册表。

`~/.redpanda/workspaces.json` 是工作区清单的唯一事实源：一条记录 = 一个沙箱边界
（一个 task root，外加是否挂载宿主机文件系统）。会话通过 `workspace_id` 绑定到
其中一条记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import uuid

from redpanda.sandbox.local.provider import discover_host_roots
from redpanda.sandbox.workspace import (
    EnvironmentInputError,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)

TASK_ROOT_ID = "project"
REGISTRY_VERSION = 1
WORKSPACE_ID_PREFIX = "workspace-"


class WorkspaceRegistryError(EnvironmentInputError):
    code = "WORKSPACE_REGISTRY_ERROR"


class WorkspaceNotFound(EnvironmentInputError):
    code = "WORKSPACE_NOT_FOUND"

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"未知的工作区: {workspace_id}")


class WorkspacePathTaken(EnvironmentInputError):
    code = "WORKSPACE_PATH_TAKEN"

    def __init__(self, path: Path) -> None:
        super().__init__(f"该路径已经是一个工作区: {path}")


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: str
    name: str
    task_root: Path
    full_access: bool
    created_at: str

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not str or not self.workspace_id.startswith(
            WORKSPACE_ID_PREFIX
        ):
            raise ValueError(
                f"workspace_id 必须以 {WORKSPACE_ID_PREFIX} 开头"
            )
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("工作区 name 必须是非空 string")
        if type(self.full_access) is not bool:
            raise ValueError("full_access 必须是 bool")
        if type(self.created_at) is not str or not self.created_at.strip():
            raise ValueError("created_at 必须是非空 string")
        resolved = self.task_root.resolve()
        if not resolved.is_dir():
            raise ValueError(f"工作区路径不是已存在的目录: {resolved}")
        object.__setattr__(self, "task_root", resolved)

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "task_root": str(self.task_root),
            "full_access": self.full_access,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "WorkspaceRecord":
        if not isinstance(value, dict) or set(value) != {
            "workspace_id",
            "name",
            "task_root",
            "full_access",
            "created_at",
        }:
            raise ValueError("workspace 记录字段不匹配")
        for field in ("workspace_id", "name", "task_root", "created_at"):
            if type(value[field]) is not str:
                raise ValueError(f"workspace 记录 {field} 必须是 string")
        if type(value["full_access"]) is not bool:
            raise ValueError("workspace 记录 full_access 必须是 bool")
        return cls(
            workspace_id=value["workspace_id"],
            name=value["name"],
            task_root=Path(value["task_root"]),
            full_access=value["full_access"],
            created_at=value["created_at"],
        )


class WorkspaceRegistry:
    """`workspaces.json` 的读写门面，一个实例对应一个文件。"""

    def __init__(
        self,
        path: Path,
        workspaces: tuple[WorkspaceRecord, ...] = (),
    ) -> None:
        self.path = path
        self._workspaces = list(workspaces)

    @property
    def workspaces(self) -> tuple[WorkspaceRecord, ...]:
        return tuple(self._workspaces)

    @classmethod
    def load(cls, path: Path) -> "WorkspaceRegistry":
        if not path.exists():
            return cls(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"version", "workspaces"}:
            raise WorkspaceRegistryError(f"{path} 顶层字段不匹配")
        if raw["version"] != REGISTRY_VERSION:
            raise WorkspaceRegistryError(
                f"不支持的 workspaces.json version: {raw['version']!r}"
            )
        entries = raw["workspaces"]
        if not isinstance(entries, list):
            raise WorkspaceRegistryError("workspaces 必须是数组")
        records = tuple(WorkspaceRecord.from_dict(entry) for entry in entries)
        identifiers = [record.workspace_id for record in records]
        if len(set(identifiers)) != len(identifiers):
            raise WorkspaceRegistryError("工作区 id 不能重复")
        roots = [record.task_root for record in records]
        if len(set(roots)) != len(roots):
            raise WorkspaceRegistryError("同一路径不能注册为多个工作区")
        return cls(path, records)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "workspaces": [record.to_dict() for record in self._workspaces],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def get(self, workspace_id: str) -> WorkspaceRecord:
        for record in self._workspaces:
            if record.workspace_id == workspace_id:
                return record
        raise WorkspaceNotFound(workspace_id)

    def find_by_path(self, path: Path) -> WorkspaceRecord | None:
        """按最深匹配找 path 所属的工作区；不属于任何工作区时返回 None。"""
        resolved = path.resolve()
        matches = [
            record
            for record in self._workspaces
            if resolved.is_relative_to(record.task_root)
        ]
        if not matches:
            return None
        return max(matches, key=lambda record: len(record.task_root.parts))

    def latest_created(self) -> WorkspaceRecord | None:
        if not self._workspaces:
            return None
        return max(
            self._workspaces,
            key=lambda record: (
                datetime.fromisoformat(record.created_at),
                record.workspace_id,
            ),
        )

    def create(
        self,
        *,
        name: str,
        task_root: Path,
        full_access: bool = False,
    ) -> WorkspaceRecord:
        resolved = task_root.resolve()
        if not resolved.is_dir():
            raise WorkspaceRegistryError(f"工作区路径不是已存在的目录: {resolved}")
        if any(record.task_root == resolved for record in self._workspaces):
            raise WorkspacePathTaken(resolved)
        record = WorkspaceRecord(
            workspace_id=f"{WORKSPACE_ID_PREFIX}{uuid.uuid4().hex}",
            name=name,
            task_root=resolved,
            full_access=full_access,
            created_at=datetime.now().astimezone().isoformat(),
        )
        self._workspaces.append(record)
        self.save()
        return record

    def register_path(
        self,
        path: Path,
        *,
        name: str | None = None,
    ) -> WorkspaceRecord:
        """按启动路径隐式登记：已属于某个工作区则复用，否则新建（名字取目录名）。"""
        existing = self.find_by_path(path)
        if existing is not None:
            return existing
        resolved = path.resolve()
        return self.create(
            name=name or resolved.name or str(resolved),
            task_root=resolved,
        )


def workspace_view(record: WorkspaceRecord) -> WorkspaceViewSnapshot:
    """把一个工作区记录转成沙箱的 Workspace View。"""
    roots = [
        RootBinding(
            root_id=TASK_ROOT_ID,
            scope=WorkspaceScope.TASK,
            path=record.task_root,
        )
    ]
    if record.full_access:
        roots.extend(discover_host_roots())
    return WorkspaceViewSnapshot(tuple(roots))
