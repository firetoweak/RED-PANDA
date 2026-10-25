import tempfile
import unittest
from pathlib import Path
from konden.filesystem import FileAccess
from konden.filesystem.workspace import (
    EnvironmentLocation,
    FilesystemPermission,
    PathOutsideWorkspaceView,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)


class EnvironmentPathContractTest(unittest.TestCase):
    def binding(self, root: Path, *, cwd: Path | None = None) -> FileAccess:
        view = WorkspaceViewSnapshot((
            RootBinding("project", WorkspaceScope.TASK, root),
        ))
        return FileAccess(
            environment_id='local-test',
            workspace_view=view,
            permission_binding=PermissionBinding((('project', FilesystemPermission.READ_WRITE),)),
            cwd=cwd or root,
        )

    def test_relative_path_resolves_from_binding_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "src"
            cwd.mkdir()

            resolved = self.binding(root, cwd=cwd).resolver.resolve(
                "../docs/new.txt"
            )

            self.assertEqual(resolved.native_path, (root / "docs" / "new.txt").resolve())
            self.assertEqual(resolved.workspace_membership.root_id, "project")
            self.assertEqual(
                resolved.location,
                EnvironmentLocation("local-test", resolved.native_path.as_uri()),
            )

    def test_absolute_environment_path_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "inside.txt"

            resolved = self.binding(root).resolver.resolve(str(target))

            self.assertEqual(resolved.native_path, target.resolve())

    def test_nonexistent_path_still_has_a_location(self):
        with tempfile.TemporaryDirectory() as directory:
            resolved = self.binding(Path(directory)).resolver.resolve(
                "new/not-exist.txt"
            )

            self.assertFalse(resolved.native_path.exists())
            self.assertEqual(resolved.location.path, resolved.native_path.as_uri())

    def test_rejects_path_outside_workspace_view(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()

            with self.assertRaises(PathOutsideWorkspaceView):
                self.binding(root).resolver.resolve("../outside.txt")

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "link").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"当前环境无法创建符号链接: {exc}")

            with self.assertRaises(PathOutsideWorkspaceView):
                self.binding(root).resolver.resolve("link/new.txt")

    def test_nested_roots_choose_the_most_specific_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            package = project / "package"
            package.mkdir()
            view = WorkspaceViewSnapshot((
                RootBinding("project", WorkspaceScope.TASK, project),
                RootBinding("package", WorkspaceScope.TASK, package),
            ))

            membership = view.membership(package / "a.py")

            self.assertEqual(membership.root_id, "package")
            self.assertEqual(membership.display_path, "a.py")
