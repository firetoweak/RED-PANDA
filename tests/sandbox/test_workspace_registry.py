import json
import tempfile
import unittest
from pathlib import Path

from redpanda.sandbox.registry import (
    REGISTRY_VERSION,
    WorkspaceNotFound,
    WorkspacePathTaken,
    WorkspaceRecord,
    WorkspaceRegistry,
    WorkspaceRegistryError,
    workspace_view,
)
from redpanda.sandbox.workspace import WorkspaceScope


class WorkspaceRegistryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.registry_path = self.root / "redpanda" / "workspaces.json"

    def make_project(self, name: str) -> Path:
        project = self.root / name
        project.mkdir()
        return project

    def workspace_at(self, registry: WorkspaceRegistry, path: Path) -> WorkspaceRecord:
        record = registry.find_by_path(path)
        self.assertIsNotNone(record)
        assert record is not None
        return record

    def test_missing_file_is_an_empty_registry(self):
        registry = WorkspaceRegistry.load(self.registry_path)

        self.assertEqual(registry.workspaces, ())
        self.assertIsNone(registry.latest_created())
        self.assertFalse(self.registry_path.exists())

    def test_create_persists_and_reloads(self):
        registry = WorkspaceRegistry.load(self.registry_path)
        project = self.make_project("my-app")

        record = registry.create(name="my-app", task_root=project)

        self.assertTrue(record.workspace_id.startswith("workspace-"))
        self.assertEqual(record.task_root, project.resolve())
        self.assertFalse(record.full_access)
        self.assertEqual(
            WorkspaceRegistry.load(self.registry_path).workspaces,
            (record,),
        )
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], REGISTRY_VERSION)
        self.assertEqual(payload["workspaces"][0]["name"], "my-app")

    def test_get_rejects_unknown_workspace(self):
        registry = WorkspaceRegistry.load(self.registry_path)

        with self.assertRaises(WorkspaceNotFound):
            registry.get("workspace-missing")

    def test_duplicate_path_is_rejected(self):
        registry = WorkspaceRegistry.load(self.registry_path)
        project = self.make_project("my-app")
        registry.create(name="my-app", task_root=project)

        with self.assertRaises(WorkspacePathTaken):
            registry.create(name="again", task_root=project)

    def test_nested_path_belongs_to_the_deepest_workspace(self):
        registry = WorkspaceRegistry.load(self.registry_path)
        outer = self.make_project("outer")
        inner = outer / "inner"
        inner.mkdir()
        outer_record = registry.create(name="outer", task_root=outer)
        inner_record = registry.create(name="inner", task_root=inner)

        self.assertEqual(
            self.workspace_at(registry, inner).workspace_id,
            inner_record.workspace_id,
        )
        self.assertEqual(
            self.workspace_at(registry, outer).workspace_id,
            outer_record.workspace_id,
        )
        self.assertIsNone(registry.find_by_path(self.root / "elsewhere"))

    def test_register_path_reuses_the_enclosing_workspace(self):
        registry = WorkspaceRegistry.load(self.registry_path)
        project = self.make_project("my-app")

        first = registry.register_path(project)
        again = registry.register_path(project / "src")

        self.assertEqual(first.workspace_id, again.workspace_id)
        self.assertEqual(first.name, "my-app")
        self.assertEqual(len(registry.workspaces), 1)

    def test_create_rejects_a_missing_directory(self):
        registry = WorkspaceRegistry.load(self.registry_path)

        with self.assertRaises(WorkspaceRegistryError):
            registry.create(name="gone", task_root=self.root / "missing")

    def test_register_path_names_new_workspace_after_directory(self):
        registry = WorkspaceRegistry.load(self.registry_path)
        project = self.make_project("brand-new")

        record = registry.register_path(project)

        self.assertEqual(record.name, "brand-new")
        self.assertEqual(record.task_root, project.resolve())

    def test_latest_created_picks_the_most_recent(self):
        registry = WorkspaceRegistry.load(self.registry_path)
        first = registry.create(name="first", task_root=self.make_project("first"))
        second = registry.create(name="second", task_root=self.make_project("second"))

        self.assertEqual(registry.latest_created(), second)
        self.assertNotEqual(first.workspace_id, second.workspace_id)

    def test_record_rejects_missing_directory(self):
        with self.assertRaises(ValueError):
            WorkspaceRecord(
                workspace_id="workspace-1",
                name="gone",
                task_root=self.root / "gone",
                full_access=False,
                created_at="2026-09-18T10:00:00+08:00",
            )

    def test_registry_rejects_unknown_version(self):
        self.registry_path.parent.mkdir(parents=True)
        self.registry_path.write_text(
            json.dumps({"version": 99, "workspaces": []}),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            WorkspaceRegistry.load(self.registry_path)

    def test_registry_rejects_record_with_wrong_fields(self):
        self.registry_path.parent.mkdir(parents=True)
        self.registry_path.write_text(
            json.dumps(
                {
                    "version": REGISTRY_VERSION,
                    "workspaces": [{"workspace_id": "workspace-1", "name": "x"}],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            WorkspaceRegistry.load(self.registry_path)


class WorkspaceViewTest(unittest.TestCase):
    def record(self, root: Path, *, full_access: bool) -> WorkspaceRecord:
        return WorkspaceRecord(
            workspace_id="workspace-1",
            name="project",
            task_root=root,
            full_access=full_access,
            created_at="2026-09-18T10:00:00+08:00",
        )

    def test_view_contains_only_the_task_root_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            view = workspace_view(self.record(Path(directory), full_access=False))

            self.assertEqual(len(view.roots), 1)
            self.assertEqual(view.roots[0].root_id, "project")
            self.assertIs(view.roots[0].scope, WorkspaceScope.TASK)

    def test_full_access_adds_host_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            view = workspace_view(self.record(Path(directory), full_access=True))

            self.assertEqual(view.roots[0].root_id, "project")
            self.assertTrue(
                any(root.scope is WorkspaceScope.HOST for root in view.roots)
            )
