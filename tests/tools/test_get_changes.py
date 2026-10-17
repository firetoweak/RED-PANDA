from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from redpanda.filesystem import FileAccess
from redpanda.filesystem.workspace import (
    FilesystemPermission, PermissionBinding, RootBinding, WorkspaceScope, WorkspaceViewSnapshot,
)
from pydantic import ValidationError
from redpanda.tools.builtin.get_changes import GetChangesInput, create_get_changes_specs


class GetChangesToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.session_operations = AsyncMock(return_value=("a" * 64,))
        self.filesystem = SimpleNamespace(
            root=self.root, native_path=lambda path: path, logical_path=lambda path: path,
            changes=AsyncMock(return_value={
                "ok": True, "code": "CHANGES_READ", "scope": "session",
                "changes": [{"path": "file.txt", "origin": "agent", "edits": [
                    {"origin": "agent", "byte_offset": 0, "before": "old", "after": "new", "truncated": False},
                ], "properties": [], "content_complete": True, "limitations": [], "truncated": False}],
                "content_complete": True, "truncated": False,
            }),
        )
        self.binding = FileAccess(
            environment_id='local-test',
            workspace_view=WorkspaceViewSnapshot((RootBinding('project', WorkspaceScope.TASK, self.root),)),
            permission_binding=PermissionBinding((('project', FilesystemPermission.READ_WRITE),)),
            cwd=self.root,
            filesystem=self.filesystem,
        )

    def tearDown(self):
        self.directory.cleanup()

    async def query(self, path=".", *, binding=None, scope="session"):
        return await create_get_changes_specs(binding or self.binding, self.session_operations)[0].handler(GetChangesInput(path=path, scope=scope))

    async def test_delegates_to_file_management(self):
        result = await self.query("file.txt")
        self.filesystem.changes.assert_awaited_once_with(self.root / "file.txt", operations=("a" * 64,), offset=0, text_offset=0)
        self.session_operations.assert_awaited_once_with()
        item = result["changes"][0]
        self.assertEqual(item["edits"][0]["after"], "new")
        self.assertNotIn("repository_location", result)

    async def test_workspace_scope_is_explicit_and_does_not_query_session_operations(self):
        await self.query(scope="workspace")
        self.filesystem.changes.assert_awaited_once_with(self.root, operations=None, offset=0, text_offset=0)
        self.session_operations.assert_not_awaited()
        with self.assertRaises(ValidationError):
            GetChangesInput(scope="all")

    async def test_unavailable_file_tracking_does_not_claim_a_clean_workspace(self):
        binding = replace(self.binding, filesystem=None)
        result = await self.query(binding=binding)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CHANGES_UNAVAILABLE")
        self.filesystem.changes.assert_not_awaited()

    async def test_rejects_paths_outside_the_workspace_before_querying(self):
        result = await self.query(str(self.root.parent / "outside.txt"))
        self.assertFalse(result["ok"])
        self.filesystem.changes.assert_not_awaited()

    async def test_filesystem_failure_is_reported_without_a_success_claim(self):
        self.filesystem.changes.side_effect = OSError("文件在查询期间发生变化")
        result = await self.query()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CHANGES_READ_FAILED")

    async def test_corrupt_evidence_is_not_converted_to_an_ordinary_failure(self):
        self.filesystem.changes.side_effect = ValueError("invalid receipt")
        with self.assertRaisesRegex(ValueError, "invalid receipt"):
            await self.query()
