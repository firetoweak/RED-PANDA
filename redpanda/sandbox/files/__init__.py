"""Sandbox file-management entry points; storage and merge backends stay internal."""
from .children import ChildFiles, child_root, child_workspace
from .changes import read_changes, ChangesReadConflict
from .text import read_text, write_text, replace_text
from .operations import (
    SandboxUnavailable,
    UnknownWorkspaceVersion,
    WorkspaceFiles,
    WorkspaceRestore,
    WorkspaceRestoreFailed,
    operation_id,
    validate_version,
    workspace_files,
)

__all__ = [
    "ChildFiles",
    "ChangesReadConflict",
    "SandboxUnavailable",
    "UnknownWorkspaceVersion",
    "WorkspaceFiles",
    "WorkspaceRestore",
    "WorkspaceRestoreFailed",
    "child_root",
    "child_workspace",
    "operation_id",
    "read_changes",
    "read_text",
    "write_text",
    "replace_text",
    "validate_version",
    "workspace_files",
]
