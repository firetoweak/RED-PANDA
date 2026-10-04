from __future__ import annotations

from pathlib import Path

from redpanda.sandbox.registry import WorkspaceRecord


def workspace_record(
    path: Path,
    *,
    name: str = "test-workspace",
    full_access: bool = False,
) -> WorkspaceRecord:
    """测试用的工作区记录：不落盘，只满足装配所需的契约。"""
    return WorkspaceRecord(
        workspace_id="workspace-" + "0" * 32,
        name=name,
        task_root=path,
        full_access=full_access,
        created_at="2026-01-01T00:00:00+00:00",
    )
