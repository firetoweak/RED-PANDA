from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from konden.assistant.catalog import CatalogSkill
from konden.assistant.context.projection import ModelContextSettings
from konden.assistant.skills import SkillToolAdapter
from konden.paths import KondenHome
from konden.skills.application import SkillApplicationService
from konden.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE
from tests.fixtures.artifacts import MemoryArtifactGateway
from tests.skills.test_package import write_skill


class SkillToolAdapterTest(unittest.IsolatedAsyncioTestCase):
    SESSION_ID = "skill-session"

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = KondenHome(root / ".konden")
        self.workspace.initialize()
        source = root / "source"
        write_skill(
            source,
            name="demo",
            description="Demo workflow",
            body="\nFollow the demo workflow.\n",
        )
        reference = source / "references" / "guide.md"
        reference.parent.mkdir()
        reference.write_text("abcdefghij", encoding="utf-8")
        self.service = SkillApplicationService(self.workspace)
        await self.service.install_local(source)
        await self.service.set_enabled("demo", True)
        self.adapter = SkillToolAdapter(
            type(
                "Skills",
                (),
                {"tool_catalog": self.service.tool_catalog},
            )(),
            MemoryArtifactGateway(),
            ModelContextSettings(),
        )
        self.adapter.apply_catalog(
            self.SESSION_ID,
            tuple(
                CatalogSkill(record.name, record.description, record.revision)
                for record in self.service.registry.snapshot()
                if record.enabled
            ),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_bundled_skills_are_available_without_installation_or_project(self):
        home = KondenHome(Path(self.temporary.name) / "empty-home")
        home.initialize()
        service = SkillApplicationService(home)
        adapter = SkillToolAdapter(
            type("Skills", (), {"tool_catalog": service.tool_catalog})(),
            MemoryArtifactGateway(), ModelContextSettings(),
        )
        catalog = adapter.registry_catalog()
        self.assertEqual({item["id"] for item in catalog}, {"builtin:coding", "builtin:git-collaboration", "builtin:web-research", "builtin:independent-verification", "builtin:konden-guide"})
        self.assertEqual(service.registry.snapshot(), ())
        adapter.apply_catalog(self.SESSION_ID, tuple(CatalogSkill(**item) for item in catalog))
        context = type("Context", (), {"session_id": self.SESSION_ID})()
        for item in catalog:
            loaded = await adapter.bindings()[LOAD_SKILL].handler(context, {"skill_id": item["id"]})
            self.assertEqual(loaded["code"], "SKILL_LOADED")
            directory = Path(loaded["data"]["skill_dir"])
            bundle = service.tool_catalog.package_reader.read(directory)
            self.assertEqual(loaded["data"]["content"], bundle.main_instructions)
            resource = await adapter.bindings()[READ_SKILL_RESOURCE].handler(
                context, {"skill_id": item["id"], "relative_path": "SKILL.md"},
            )
            self.assertEqual(resource["data"]["content"], (directory / "SKILL.md").read_text(encoding="utf-8"))

    async def test_project_skills_come_from_the_task_root_and_follow_its_changes(self):
        project = Path(self.temporary.name) / "project"
        skills_root = project / ".claude" / "skills"
        write_skill(skills_root / "verify", name="verify", description="Verify the app",
                    body="\nRun the checks.\n")
        (skills_root / "broken").mkdir()
        adapter = SkillToolAdapter(
            type("Skills", (), {"tool_catalog": self.service.tool_catalog})(),
            MemoryArtifactGateway(),
            ModelContextSettings(),
            project_root=project,
        )
        catalog = adapter.registry_catalog()
        by_id = {item["id"]: item for item in catalog}
        self.assertEqual(set(by_id), {"demo", "builtin:coding", "builtin:git-collaboration", "builtin:web-research", "builtin:independent-verification", "builtin:konden-guide", "project:broken", "project:verify"})
        self.assertEqual(by_id["project:verify"]["description"], "Verify the app")
        self.assertIn("SKILL.md", by_id["project:broken"]["description"])
        adapter.apply_catalog(self.SESSION_ID, tuple(CatalogSkill(**item) for item in catalog))
        context = type("Context", (), {"session_id": self.SESSION_ID})()
        load = adapter.bindings()[LOAD_SKILL].handler

        loaded = await load(context, {"skill_id": "project:verify"})
        self.assertTrue(loaded["ok"])
        self.assertIn("Run the checks.", loaded["data"]["content"])
        self.assertEqual(Path(loaded["data"]["skill_dir"]), (skills_root / "verify").resolve())
        self.assertEqual((await load(context, {"skill_id": "project:broken"}))["code"], "SKILL_PACKAGE_INVALID")

        write_skill(skills_root / "verify", name="verify", description="Verify the app",
                    body="\nRun the new checks.\n")
        self.assertEqual((await load(context, {"skill_id": "project:verify"}))["code"], "SKILL_CATALOG_STALE")

    async def test_project_skills_beyond_the_catalog_budget_are_listed_unavailable(self):
        project = Path(self.temporary.name) / "crowded"
        for index in range(8):
            write_skill(project / ".claude" / "skills" / f"big-{index:02}", name=f"big-{index:02}",
                        description="x" * 500)
        constrained = SkillApplicationService(self.workspace, max_catalog_chars=4000)
        adapter = SkillToolAdapter(
            type("Skills", (), {"tool_catalog": constrained.tool_catalog})(),
            MemoryArtifactGateway(), ModelContextSettings(), project_root=project,
        )
        catalog = adapter.registry_catalog()
        by_id = {item["id"]: item["description"] for item in catalog}
        self.assertEqual(set(by_id), {"demo", "builtin:coding", "builtin:git-collaboration", "builtin:web-research", "builtin:independent-verification", "builtin:konden-guide"} | {f"project:big-{index:02}" for index in range(8)})
        self.assertLessEqual(len("\n".join(f"- {key}: {value}" for key, value in by_id.items())), 4000)
        self.assertEqual(by_id["project:big-00"], "x" * 500)
        self.assertIn("长度上限", by_id["project:big-07"])
        adapter.apply_catalog(self.SESSION_ID, tuple(CatalogSkill(**item) for item in catalog))
        context = type("Context", (), {"session_id": self.SESSION_ID})()
        result = await adapter.bindings()[LOAD_SKILL].handler(context, {"skill_id": "project:big-07"})
        self.assertEqual(result["code"], "SKILL_PACKAGE_INVALID")

    async def test_project_skills_directory_leading_outside_the_task_root_is_not_read(self):
        outside = Path(self.temporary.name) / "outside-skills"
        write_skill(outside / "elsewhere", name="elsewhere")
        linked = Path(self.temporary.name) / "linked"
        (linked / ".claude").mkdir(parents=True)
        try:
            os.symlink(outside, linked / ".claude" / "skills", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("current platform cannot create symlinks")
        linked_adapter = SkillToolAdapter(
            type("Skills", (), {"tool_catalog": self.service.tool_catalog})(),
            MemoryArtifactGateway(), ModelContextSettings(), project_root=linked,
        )
        ids = {item["id"] for item in linked_adapter.registry_catalog()}
        self.assertNotIn("project:elsewhere", ids)
        self.assertIn("project:.claude/skills", ids)

    async def test_crowded_project_catalog_preserves_every_id_and_explains_unavailability(self):
        project = Path(self.temporary.name) / "unavailable-skills"
        for index in range(3):
            name = f"big-{index:02}"
            write_skill(project / ".claude" / "skills" / name, name=name, description="x" * 500)
        constrained = SkillApplicationService(self.workspace, max_catalog_chars=400)
        adapter = SkillToolAdapter(
            type("Skills", (), {"tool_catalog": constrained.tool_catalog})(),
            MemoryArtifactGateway(), ModelContextSettings(), project_root=project,
        )
        catalog = adapter.registry_catalog()
        by_id = {item["id"]: item for item in catalog}
        self.assertEqual(set(by_id), {"demo", "builtin:coding", "builtin:git-collaboration", "builtin:web-research", "builtin:independent-verification", "builtin:konden-guide"} | {f"project:big-{index:02}" for index in range(3)})
        adapter.apply_catalog(self.SESSION_ID, tuple(CatalogSkill(**item) for item in catalog))
        context = type("Context", (), {"session_id": self.SESSION_ID})()
        for index in range(3):
            skill_id = f"project:big-{index:02}"
            self.assertIn("长度上限", by_id[skill_id]["description"])
            result = await adapter.bindings()[LOAD_SKILL].handler(context, {"skill_id": skill_id})
            self.assertEqual(result["code"], "SKILL_PACKAGE_INVALID")
            self.assertIn("长度上限", result["error"])

    async def test_disable_keeps_skill_tool_definitions_stable(self):
        before = self.adapter.schemas()
        await self.service.set_enabled("demo", False)
        self.assertEqual(self.adapter.schemas(), before)

        result = await self.adapter.bindings()[LOAD_SKILL].handler(
            type("Context", (), {"session_id": self.SESSION_ID})(),
            {"skill_id": "demo"},
        )
        self.assertEqual(result["code"], "SKILL_CATALOG_STALE")

    async def test_load_skill_returns_main_instructions_as_a_tool_result(self):
        context = type("Context", (), {"session_id": self.SESSION_ID})()
        result = await self.adapter.bindings()[LOAD_SKILL].handler(context, {"skill_id": "demo"})
        self.assertEqual(result["code"], "SKILL_LOADED")
        self.assertEqual(result["data"]["content"], "\nFollow the demo workflow.\n")

    async def test_read_skill_resource_does_not_require_a_prior_load(self):
        context = type("Context", (), {"session_id": self.SESSION_ID})()
        result = await self.adapter.bindings()[READ_SKILL_RESOURCE].handler(
            context, {"skill_id": "demo", "relative_path": "references/guide.md", "offset": 2, "limit": 4},
        )
        self.assertEqual(result["code"], "SKILL_RESOURCE_READ")
        self.assertEqual(result["data"]["content"], "cdef")

    async def test_tool_spec_validation_uses_validation_error_code(self):
        binding = self.adapter.bindings()[LOAD_SKILL]

        result = await binding.handler(
            type(
                "Context",
                (),
                {"session_id": self.SESSION_ID},
            )(),
            {},
        )

        self.assertEqual(result["code"], "VALIDATION_ERROR")
