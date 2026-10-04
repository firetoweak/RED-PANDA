import tempfile
import unittest
import json
from pathlib import Path

from redpanda.paths import RedPandaHome
from redpanda.skills.installer import LocalSkillInstaller
from redpanda.skills.models import SkillRecord, SkillSourceRef
from redpanda.skills.registry import SkillRegistry
from tests.skills.test_package import write_skill


class FailingRegistry:
    async def get(self, _skill_id):
        return None

    async def add(self, _record):
        raise RuntimeError("registry write failed")


class SkillRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_requires_current_version_and_complete_record(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SkillRegistry(Path(directory) / "skills")
            record = SkillRecord(
                name="demo",
                description="demo",
                source=SkillSourceRef("local", "/demo"),
                resolved_ref="file:///demo",
                content_hash="hash",
            ).to_dict()
            registry.root.mkdir(parents=True)

            registry.path.write_text(
                json.dumps({"version": 0, "skills": [record]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                await registry.list_skills()

            record.pop("revision")
            registry.path.write_text(
                json.dumps({"version": 1, "skills": [record]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                await registry.list_skills()

    async def test_persists_sorted_records_and_revisioned_enabled_state(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = SkillRegistry(Path(directory) / "skills")
            for name in ("zeta", "alpha"):
                await registry.add(SkillRecord(
                    name=name,
                    description=name,
                    source=SkillSourceRef("local", f"/{name}"),
                    resolved_ref=f"file:///{name}",
                    content_hash=name * 8,
                ))

            stored = await registry.list_skills()
            self.assertEqual([item.name for item in stored], ["alpha", "zeta"])
            enabled = await registry.set_enabled("alpha", True)
            self.assertTrue(enabled.enabled)
            self.assertEqual(enabled.revision, 2)
            self.assertEqual(enabled.created_at, stored[0].created_at)
            self.assertEqual(
                (await registry.get("alpha")).content_hash,
                "alpha" * 8,
            )

            same = await registry.set_enabled("alpha", True)
            self.assertEqual(same.revision, 2)


class LocalSkillInstallerTest(unittest.IsolatedAsyncioTestCase):
    async def test_installs_under_frontmatter_name_and_defaults_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = RedPandaHome(Path(directory) / ".redpanda")
            workspace.initialize()
            skills_root = workspace.skills_root
            source = Path(directory) / "source-directory-name-differs"
            write_skill(source, name="python-testing")
            script = source / "scripts" / "run.py"
            script.parent.mkdir()
            script.write_text("print('ok')\n", encoding="utf-8")
            registry = SkillRegistry(skills_root)
            installer = LocalSkillInstaller(skills_root, registry)

            record = await installer.install(source)

            target = skills_root / "packages" / "python-testing"
            self.assertEqual(record.name, "python-testing")
            self.assertTrue(record.enabled)
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertTrue((target / "scripts" / "run.py").is_file())
            self.assertEqual((await registry.get(record.name)), record)
            self.assertEqual(list(installer.staging_root.iterdir()), [])

    async def test_registry_failure_rolls_back_published_package(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = RedPandaHome(Path(directory) / ".redpanda")
            workspace.initialize()
            skills_root = workspace.skills_root
            source = Path(directory) / "source"
            write_skill(source, name="demo")
            installer = LocalSkillInstaller(
                skills_root,
                FailingRegistry(),
            )

            with self.assertRaisesRegex(RuntimeError, "registry write failed"):
                await installer.install(source)

            self.assertFalse(
                (skills_root / "packages" / "demo").exists()
            )
            self.assertEqual(list(installer.staging_root.iterdir()), [])

    async def test_orphan_target_is_contract_error_not_silently_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = RedPandaHome(Path(directory) / ".redpanda")
            workspace.initialize()
            skills_root = workspace.skills_root
            source = Path(directory) / "source"
            write_skill(source, name="demo")
            orphan = skills_root / "packages" / "demo"
            orphan.mkdir(parents=True)
            installer = LocalSkillInstaller(
                skills_root,
                SkillRegistry(skills_root),
            )

            with self.assertRaisesRegex(RuntimeError, "未登记"):
                await installer.install(source)

    async def test_duplicate_registry_record_does_not_replace_package(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = RedPandaHome(Path(directory) / ".redpanda")
            workspace.initialize()
            skills_root = workspace.skills_root
            source = Path(directory) / "source"
            write_skill(source, name="demo")
            registry = SkillRegistry(skills_root)
            installer = LocalSkillInstaller(skills_root, registry)
            await installer.install(source)
            installed = skills_root / "packages" / "demo" / "SKILL.md"
            original = installed.read_bytes()
            (source / "SKILL.md").write_text(
                "---\nname: demo\ndescription: changed\n---\nchanged\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "已安装"):
                await installer.install(source)

            self.assertEqual(installed.read_bytes(), original)
