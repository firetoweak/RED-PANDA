from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from redpanda.filesystem import FileAccess
from redpanda.filesystem.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from redpanda.tools.builtin.file_read import (
    GlobInput,
    GrepInput,
    ReadFileInput,
    _matches_glob,
    create_file_read_specs,
)


def _binding(root: Path) -> FileAccess:
    view = WorkspaceViewSnapshot((
        RootBinding("project", WorkspaceScope.TASK, root),
    ))
    return FileAccess(
        environment_id='local-test',
        workspace_view=view,
        permission_binding=PermissionBinding((('project', FilesystemPermission.READ_WRITE),)),
        cwd=root,
    )


def _handlers(root: Path):
    specs = {spec.name: spec for spec in create_file_read_specs(_binding(root))}
    return specs


class FileSearchContractTest(unittest.TestCase):
    def test_glob_double_star_matches_zero_or_multiple_directory_levels(self):
        for path in ("a.py", "src/a.py", "src/nested/deeper/a.py"):
            with self.subTest(path=path):
                self.assertTrue(_matches_glob(path, "**/*.py"))
                self.assertTrue(_matches_glob(path, "**/a.py"))
        self.assertTrue(_matches_glob("src/nested/a.py", "src/**/a.py"))
        self.assertTrue(_matches_glob("src/a.py", "src/**/a.py"))
        self.assertFalse(_matches_glob("src/nested/a.py", "src/*.py"))
        self.assertFalse(_matches_glob("other/src/a.py", "src/*.py"))
        self.assertFalse(_matches_glob("src/a.txt", "**/*.py"))

    def test_descriptions_explain_hidden_and_gitignore_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            specs = _handlers(Path(directory))
        for name in ("glob", "grep"):
            text = specs[name].description
            self.assertIn("隐藏", text)
            self.assertIn("gitignore", text)
            self.assertIn("include_hidden", text)
            self.assertIn("include_ignored", text)


class ReadFilePagingTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_execution_deadline_comes_from_tool_arguments(self):
        timeout = asyncio.timeout
        for name, raw, code in (
            ("glob", GlobInput(pattern="*", timeout_seconds=17), "GLOB_TIMEOUT"),
            ("grep", GrepInput(query="needle", timeout_seconds=23), "RG_TIMEOUT"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                async def blocked(*args):
                    await asyncio.Future()

                proc = SimpleNamespace(
                    returncode=None,
                    communicate=AsyncMock(side_effect=blocked),
                    stdout=SimpleNamespace(readline=AsyncMock(side_effect=blocked)),
                    stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
                    wait=AsyncMock(return_value=-15),
                )
                proc.terminate = lambda: setattr(proc, "returncode", -15)
                with (
                    patch("redpanda.tools.builtin.file_read.shutil.which", return_value=name),
                    patch("redpanda.tools.builtin.file_read.asyncio.create_subprocess_exec", return_value=proc),
                    patch("redpanda.tools.builtin.file_read.asyncio.timeout", side_effect=lambda _: timeout(0)) as deadline,
                ):
                    result = await _handlers(Path(directory))[name].handler(raw)
                deadline.assert_called_once_with(raw.timeout_seconds)
                self.assertEqual(result["code"], code)
                self.assertIn(str(raw.timeout_seconds), result["error"])
                self.assertEqual(proc.returncode, -15)

    async def test_glob_rejects_empty_or_dot_pattern_without_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            glob = _handlers(Path(directory))["glob"].handler
            for pattern in ("", ".", "   "):
                with self.subTest(pattern=repr(pattern)):
                    result = await glob(GlobInput(pattern=pattern))
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["code"], "EMPTY_PATTERN")
                    self.assertNotIn("matches", result)

    async def test_glob_missing_dependency_is_an_explicit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("redpanda.tools.builtin.file_read.shutil.which", return_value=None):
                result = await _handlers(Path(directory))["glob"].handler(GlobInput(pattern="*"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "GLOB_NOT_FOUND")
        self.assertNotIn("matches", result)

    async def test_glob_traversal_errors_do_not_report_partial_results_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = SimpleNamespace(
                returncode=0,
                communicate=AsyncMock(return_value=(b"./visible.py\0", b"permission denied")),
            )
            with (
                patch("redpanda.tools.builtin.file_read.shutil.which", return_value="fd"),
                patch("redpanda.tools.builtin.file_read.asyncio.create_subprocess_exec", return_value=proc),
            ):
                result = await _handlers(Path(directory))["glob"].handler(GlobInput(pattern="*"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "GLOB_FAILED")
        self.assertEqual(result["error"], "permission denied")
        self.assertNotIn("matches", result)

    async def test_glob_resolves_only_the_selected_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "docs").mkdir()
            for name in ("a.md", "b.md"):
                (root / "docs" / name).write_text(name)
            discovered = [(f"unrelated/{index}.py", "file") for index in range(1000)]
            discovered += [("docs/a.md", "file"), ("docs/b.md", "file")]
            resolve = Path.resolve
            resolved = []
            def checked(path, *args, **kwargs):
                assert "unrelated" not in path.parts
                resolved.append(path)
                return resolve(path, *args, **kwargs)
            with (
                patch("redpanda.tools.builtin.file_read._fd_entries",
                      return_value={"ok": True, "entries": discovered}),
                patch("redpanda.tools.builtin.file_read.shutil.which", return_value="fd"),
                patch.object(Path, "resolve", checked),
            ):
                result = await _handlers(root)["glob"].handler(
                    GlobInput(pattern="docs/*.md", max_results=1))
            self.assertEqual([item["path"] for item in result["matches"]], ["docs/a.md"])
            self.assertTrue(result["truncated"])
            self.assertEqual(result["next_offset"], 1)
            self.assertNotIn(root / "docs" / "b.md", resolved)

    async def test_read_file_does_not_reject_by_total_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "big.log"
            path.write_bytes(b"first line\n")
            os.truncate(path, 20 * 1024 * 1024 + 64)
            specs = _handlers(root)
            self.assertNotIn("20 MiB", specs["read_file"].description)
            result = await specs["read_file"].handler(
                ReadFileInput(path="big.log", offset=1, limit=1),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["content"], "first line\n")
            self.assertNotEqual(result.get("code"), "FILE_TOO_LARGE")


needs_fd = unittest.skipUnless(shutil.which("fd") or shutil.which("fdfind"), "glob 需要 fd 或 fdfind")


@pytest.mark.process
class FileSearchIgnoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        subprocess.run(
            ["git", "init"],
            cwd=self.root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        (self.root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "keep.py").write_text("keep\n", encoding="utf-8")
        (self.root / "ignored").mkdir()
        (self.root / "ignored" / "secret.py").write_text(
            "secret-token\n",
            encoding="utf-8",
        )
        (self.root / ".hidden.txt").write_text("hidden-token\n", encoding="utf-8")
        (self.root / ".github").mkdir()
        (self.root / ".github" / "README.md").write_text(
            "workflow\n",
            encoding="utf-8",
        )
        git_object = self.root / ".git" / "objects" / "pack"
        git_object.mkdir(parents=True, exist_ok=True)
        (git_object / "pack-token").write_text("git-object\n", encoding="utf-8")
        specs = _handlers(self.root)
        self.glob = specs["glob"].handler
        self.grep = specs["grep"].handler

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _match_paths(self, result: dict) -> set[str]:
        return {item["path"] for item in result["matches"]}

    @needs_fd
    async def test_glob_skips_hidden_and_gitignore_by_default(self):
        result = await self.glob(GlobInput(pattern="*", max_results=100))

        self.assertTrue(result["ok"])
        paths = self._match_paths(result)
        self.assertIn("src/keep.py", paths)
        self.assertNotIn(".hidden.txt", paths)
        self.assertNotIn(".github/README.md", paths)
        self.assertNotIn("ignored/secret.py", paths)
        self.assertFalse(any(".git" in path for path in paths))
        self.assertIsNone(result["hint"])

        empty = await self.glob(GlobInput(pattern="secret.py", max_results=100))
        self.assertEqual(empty["matches"], [])
        self.assertIn("隐藏文件/目录", empty["hint"])
        self.assertIn("gitignore", empty["hint"])
        self.assertIn("include_hidden=true", empty["hint"])
        self.assertIn("include_ignored=true", empty["hint"])

    @needs_fd
    async def test_glob_include_hidden_finds_dot_files_but_not_git(self):
        result = await self.glob(
            GlobInput(pattern="*", include_hidden=True, max_results=100),
        )

        self.assertTrue(result["ok"])
        paths = self._match_paths(result)
        self.assertIn(".hidden.txt", paths)
        self.assertIn(".github/README.md", paths)
        self.assertFalse(any(path.startswith(".git/") for path in paths))

    @needs_fd
    async def test_glob_include_ignored_finds_gitignored_files(self):
        result = await self.glob(
            GlobInput(pattern="*.py", include_ignored=True, max_results=100),
        )

        self.assertTrue(result["ok"])
        paths = self._match_paths(result)
        self.assertIn("ignored/secret.py", paths)
        self.assertIn("src/keep.py", paths)

    @needs_fd
    async def test_glob_enumerates_empty_dirs_and_dirs_with_only_ignored_files(self):
        (self.root / "empty").mkdir()
        (self.root / "cache").mkdir()
        (self.root / "cache" / "only.tmp").write_text("ignored")
        with (self.root / ".gitignore").open("a") as stream:
            stream.write("*.tmp\n")
        result = await self.glob(GlobInput(pattern="*", kind="dir", max_results=100))
        self.assertTrue(result["ok"])
        self.assertEqual(self._match_paths(result), {"src", "empty", "cache"})
        self.assertTrue(all(item["kind"] == "dir" for item in result["matches"]))

    @needs_fd
    async def test_glob_max_depth_keeps_directories_with_deeper_files(self):
        (self.root / "src" / "nested").mkdir()
        (self.root / "src" / "nested" / "a.py").write_text("deep")
        result = await self.glob(GlobInput(pattern="*", max_depth=1, max_results=100))
        self.assertTrue(result["ok"])
        self.assertEqual(self._match_paths(result), {"src"})
        self.assertEqual(result["matches"][0]["kind"], "dir")

    @needs_fd
    async def test_glob_relative_patterns_and_pages_are_stable(self):
        (self.root / "src" / "nested").mkdir()
        (self.root / "src" / "nested" / "a.py").write_text("deep")
        (self.root / "src" / "A.py").write_text("upper")
        (self.root / "src" / "中文 文件.py").write_text("unicode")
        shallow = await self.glob(GlobInput(pattern="src/*.py", kind="file", max_results=100))
        self.assertEqual(self._match_paths(shallow), {"src/keep.py", "src/A.py", "src/中文 文件.py"})
        expected = ["src/A.py", "src/keep.py", "src/nested/a.py", "src/中文 文件.py"]
        for offset, path in enumerate(expected):
            with self.subTest(offset=offset):
                result = await self.glob(GlobInput(pattern="**/*.py", kind="file", offset=offset, max_results=1))
                self.assertEqual([item["path"] for item in result["matches"]], [path])
                self.assertEqual(result["next_offset"], offset + 1 if offset < len(expected) - 1 else None)

    @needs_fd
    async def test_glob_uses_fd_ignore_rules_without_overriding_gitignore(self):
        (self.root / ".fdignore").write_text("src/\n")
        result = await self.glob(GlobInput(pattern="*.py", max_results=100))
        self.assertTrue(result["ok"])
        self.assertEqual(self._match_paths(result), set())
        included = await self.glob(GlobInput(pattern="*.py", include_ignored=True, max_results=100))
        self.assertEqual(self._match_paths(included), {"src/keep.py", "ignored/secret.py"})

    @needs_fd
    async def test_explicit_path_searches_hidden_directories(self):
        for path, filename, query, include_hidden in (
            (".github", ".github/README.md", "workflow", False),
            (".git", ".git/objects/pack/pack-token", "git-object", True),
        ):
            with self.subTest(path=path):
                result = await self.glob(
                    GlobInput(
                        pattern="*", path=path, include_hidden=include_hidden,
                        max_results=100,
                    ),
                )
                self.assertTrue(result["ok"])
                self.assertIn(filename, self._match_paths(result))

                matches = await self.grep(
                    GrepInput(query=query, path=path, include_hidden=include_hidden)
                )
                self.assertTrue(matches["ok"])
                self.assertEqual({hit["file"] for hit in matches["hits"]}, {filename})

    async def test_grep_skips_hidden_and_gitignore_by_default(self):
        result = await self.grep(GrepInput(query="token", max_results=100))

        self.assertTrue(result["ok"])
        files = {hit["file"] for hit in result["hits"]}
        self.assertEqual(files, set())

        visible = await self.grep(GrepInput(query="keep", max_results=100))
        self.assertEqual(
            {hit["file"] for hit in visible["hits"]},
            {"src/keep.py"},
        )

    async def test_grep_include_hidden_and_ignored_find_skipped_files(self):
        hidden = await self.grep(
            GrepInput(query="hidden-token", include_hidden=True),
        )
        ignored = await self.grep(
            GrepInput(query="secret-token", include_ignored=True),
        )

        self.assertEqual(
            {hit["file"] for hit in hidden["hits"]},
            {".hidden.txt"},
        )
        self.assertEqual(
            {hit["file"] for hit in ignored["hits"]},
            {"ignored/secret.py"},
        )


if __name__ == "__main__":
    unittest.main()
