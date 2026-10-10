from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from redpanda.sandbox.api import EnvironmentBinding, ExecutionAttachment
from redpanda.sandbox.workspace import (
    FilesystemPermission, PermissionBinding, RootBinding, WorkspaceScope, WorkspaceViewSnapshot,
)
from redpanda.tools.builtin.get_changes import GetChangesInput, create_get_changes_specs


class GetChangesToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.filesystem = SimpleNamespace(
            root=self.root, native_path=lambda path: path, logical_path=lambda path: path,
            changes=AsyncMock(return_value={
                "ok": True, "code": "CHANGES_READ", "scope": "agent_touched_files",
                "changes": [{"path": "file.txt", "origin": "agent", "edits": [
                    {"origin": "agent", "byte_offset": 0, "before": "old", "after": "new", "truncated": False},
                ], "properties": [], "content_complete": True, "limitations": [], "truncated": False}],
                "content_complete": True, "truncated": False,
            }),
        )
        self.binding = EnvironmentBinding(
            "local-test", WorkspaceViewSnapshot((RootBinding("project", WorkspaceScope.TASK, self.root),)),
            PermissionBinding((("project", FilesystemPermission.READ_WRITE),)),
            self.root, "powershell", "pwsh.exe",
            ExecutionAttachment("local-test", object(), filesystem=self.filesystem),
        )

    def tearDown(self):
        self.directory.cleanup()

    async def query(self, path=".", *, binding=None):
        return await create_get_changes_specs(binding or self.binding)[0].handler(GetChangesInput(path=path))

    async def test_delegates_to_file_management_and_exposes_logical_locations(self):
        result = await self.query("file.txt")
        self.filesystem.changes.assert_awaited_once_with(self.root / "file.txt", offset=0, text_offset=0)
        item = result["changes"][0]
        self.assertEqual(item["location"]["path"], (self.root / "file.txt").as_uri())
        self.assertEqual(item["edits"][0]["after"], "new")
        self.assertNotIn("repository_location", result)

    async def test_unavailable_file_tracking_does_not_claim_a_clean_workspace(self):
        binding = replace(self.binding, execution_attachment=ExecutionAttachment("local-test", object()))
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
