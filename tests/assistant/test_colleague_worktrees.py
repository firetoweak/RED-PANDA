from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from redpanda.assistant.builtin_tools import build_builtin_tools
from redpanda.filesystem.registry import WorkspaceRecord


class ColleagueWorktreeAccessTest(unittest.IsolatedAsyncioTestCase):
    async def test_colleague_worktrees_are_readable_and_own_worktree_stays_writable(self):
        with TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            own, colleague = project / "ada", project / "lin"
            own.mkdir(parents=True)
            colleague.mkdir()
            (colleague / "draft.py").write_text("half done", encoding="utf-8")
            runner = await build_builtin_tools(
                WorkspaceRecord("workspace-test", "test", own, False, "2026-10-19"),
                colleague_root=project,
            )

            read = await runner.execute("read_file", {"path": str(colleague / "draft.py")})
            self.assertTrue(read["ok"])
            self.assertIn("half done", str(read["data"]))
            write = await runner.execute("write_file", {"path": str(colleague / "draft.py"),
                                                        "content": "mine", "overwrite": True})
            self.assertEqual(write["code"], "ENVIRONMENT_PERMISSION_DENIED")
            self.assertEqual((colleague / "draft.py").read_text(encoding="utf-8"), "half done")
            own_write = await runner.execute("write_file", {"path": str(own / "note.py"), "content": "mine"})
            self.assertTrue(own_write["ok"])
