from __future__ import annotations

from pathlib import Path

from konden.filesystem.registry import WorkspaceRecord


def workspace_record(
    path: Path,
    *,
    full_access: bool = False,
) -> WorkspaceRecord:
    """测试用的工作区记录：不落盘，只满足装配所需的契约。"""
    return WorkspaceRecord(
        workspace_id="workspace-" + "0" * 32,
        task_root=path,
        full_access=full_access,
        created_at="2026-01-01T00:00:00+00:00",
    )
