from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from helperme.sandbox.api import EnvironmentBinding, ExecutionAttachment
from helperme.sandbox.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from helperme.tools.builtin.get_changes import (
    GetChangesInput,
    create_get_changes_specs,
)


class GetChangesToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self._git("init")
        self._git("config", "user.name", "HelperMe Test")
        self._git("config", "user.email", "helperme@example.invalid")
        view = WorkspaceViewSnapshot((
            RootBinding("project", WorkspaceScope.TASK, self.root),
        ))
        binding = EnvironmentBinding(
            environment_id="local-test",
            workspace_view=view,
            permission_binding=PermissionBinding((
                ("project", FilesystemPermission.READ_WRITE),
            )),
            cwd=self.root,
            shell_name="powershell",
            shell_path="pwsh.exe",
            execution_attachment=ExecutionAttachment(
                "local-test",
                object(),  # get_changes 只读取本地 Git，不执行用户命令。
            ),
        )
        self.handler = create_get_changes_specs(binding)[0].handler

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _commit_file(self, path: str, content: str) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._git("add", path)
        self._git("commit", "-m", f"add {path}")
        return target

    async def _changes(self, path: str = ".") -> dict[str, object]:
        return await self.handler(GetChangesInput(path=path))

    async def test_diff_compares_head_with_final_worktree(self) -> None:
        target = self._commit_file("tracked.txt", "base\n")
        target.write_text("staged\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        target.write_text("final\n", encoding="utf-8")

        result = await self._changes()

        self.assertTrue(result["ok"])
        self.assertEqual(result["diff_basis"], "HEAD_TO_WORKTREE")
        self.assertTrue(result["baseline_revision"])
        self.assertIn("+final", result["diff"])
        self.assertNotIn("+staged", result["diff"])
        self.assertTrue(result["content_complete"])

    async def test_clean_repository_is_complete_and_unchanged(self) -> None:
        self._commit_file("tracked.txt", "base\n")

        result = await self._changes()

        self.assertFalse(result["changed"])
        self.assertEqual(result["diff"], "")
        self.assertTrue(result["content_complete"])
        self.assertEqual(result["limitations"], [])

    async def test_verification_does_not_refresh_user_index(self) -> None:
        target = self._commit_file("tracked.txt", "base\n")
        index = self.root / ".git" / "index"
        before = index.read_bytes()
        timestamp = target.stat().st_mtime + 2
        os.utime(target, (timestamp, timestamp))

        result = await self._changes()

        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(index.read_bytes(), before)

    async def test_staged_only_change_includes_content(self) -> None:
        target = self._commit_file("tracked.txt", "before\n")
        target.write_text("after\n", encoding="utf-8")
        self._git("add", "tracked.txt")

        result = await self._changes()

        self.assertIn("+after", result["diff"])
        self.assertTrue(result["content_complete"])

    async def test_untracked_path_is_explicitly_content_incomplete(self) -> None:
        self._commit_file("tracked.txt", "base\n")
        (self.root / "new file.txt").write_text("untracked secret\n", encoding="utf-8")

        result = await self._changes()

        self.assertTrue(result["changed"])
        self.assertEqual(result["untracked_paths"], ["new file.txt"])
        self.assertNotIn("untracked secret", result["diff"])
        self.assertFalse(result["content_complete"])
        self.assertIn("UNTRACKED_CONTENT_NOT_INCLUDED", result["limitations"])

    async def test_binary_change_is_explicitly_content_incomplete(self) -> None:
        target = self.root / "image.bin"
        target.write_bytes(b"\x00before")
        self._git("add", "image.bin")
        self._git("commit", "-m", "add binary")
        target.write_bytes(b"\x00after")

        result = await self._changes()

        self.assertEqual(result["binary_paths"], ["image.bin"])
        self.assertFalse(result["content_complete"])
        self.assertIn("BINARY_CONTENT_NOT_INCLUDED", result["limitations"])

    async def test_large_diff_reports_truncation_as_incomplete(self) -> None:
        target = self._commit_file("large.txt", "base\n")
        target.write_text("x" * 150_000, encoding="utf-8")

        result = await self._changes()

        self.assertTrue(result["truncated"])
        self.assertGreater(result["diff_total_chars"], len(result["diff"]))
        self.assertGreater(result["diff_omitted_chars"], 0)
        self.assertFalse(result["content_complete"])
        self.assertIn("DIFF_TRUNCATED", result["limitations"])

    async def test_repository_without_head_reports_missing_baseline(self) -> None:
        (self.root / "first.txt").write_text("first\n", encoding="utf-8")

        result = await self._changes()

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertIsNone(result["diff_basis"])
        self.assertIsNone(result["baseline_revision"])
        self.assertFalse(result["content_complete"])
        self.assertEqual(result["untracked_paths"], ["first.txt"])
        self.assertEqual(
            result["limitations"],
            [
                "HEAD_BASELINE_UNAVAILABLE",
                "UNTRACKED_CONTENT_NOT_INCLUDED",
            ],
        )
