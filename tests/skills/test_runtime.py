import tempfile
import unittest
from pathlib import Path

from redpanda.paths import RedPandaHome
from redpanda.skills.application import SkillApplicationService
from redpanda.skills.runtime import (
    LOAD_SKILL,
    READ_SKILL_RESOURCE,
    LoadSkillInput,
    ReadSkillResourceInput,
)
from tests.skills.test_package import write_skill


class SkillRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = RedPandaHome(root / ".redpanda")
        self.workspace.initialize()
        self.source = root / "source"
        write_skill(
            self.source,
            name="demo",
            description="Demo workflow",
            body="\nFollow the demo workflow.\n",
        )
        reference = self.source / "references" / "guide.md"
        reference.parent.mkdir()
        reference.write_text("abcdefghij", encoding="utf-8")
        self.service = SkillApplicationService(self.workspace)
        await self.service.install_local(self.source)
        self.record = await self.service.set_enabled("demo", True)
        self.runtime = self.service.tool_catalog

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_runtime_is_two_plain_tool_specs_and_load_returns_content(self):
        specs = {spec.name: spec for spec in self.runtime.tool_specs()}

        self.assertEqual(set(specs), {LOAD_SKILL, READ_SKILL_RESOURCE})
        self.assertNotIn("demo: Demo workflow", specs[LOAD_SKILL].description)
        self.assertTrue(specs[LOAD_SKILL].exclusive_batch)

        result = await specs[LOAD_SKILL].handler(LoadSkillInput(skill_id="demo"))

        self.assertEqual(result["code"], "SKILL_LOADED")
        self.assertEqual(result["data"]["skill_id"], "demo")
        self.assertEqual(result["data"]["revision"], self.record.revision)
        self.assertEqual(
            result["data"]["content"],
            "\nFollow the demo workflow.\n",
        )

    async def test_resource_read_requires_no_runtime_loaded_state(self):
        specs = {spec.name: spec for spec in self.runtime.tool_specs()}

        result = await specs[READ_SKILL_RESOURCE].handler(
            ReadSkillResourceInput(
                skill_id="demo",
                relative_path="references/guide.md",
                offset=2,
                limit=4,
            )
        )

        self.assertEqual(result["code"], "SKILL_RESOURCE_READ")
        self.assertEqual(result["data"]["content"], "cdef")

    async def test_specs_refresh_catalog_but_old_closure_rejects_change(self):
        old_specs = {spec.name: spec for spec in self.runtime.tool_specs()}
        await self.service.set_enabled("demo", False)

        stale = await old_specs[LOAD_SKILL].handler(
            LoadSkillInput(skill_id="demo")
        )

        self.assertEqual(stale["code"], "SKILL_CATALOG_STALE")
        self.assertEqual([s.to_openai_tool() for s in self.runtime.tool_specs()],
                         [s.to_openai_tool() for s in old_specs.values()])
