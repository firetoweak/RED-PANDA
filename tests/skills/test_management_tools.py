import tempfile
import unittest
from pathlib import Path

from redpanda.paths import RedPandaHome
from redpanda.skills.application import SkillApplicationService
from redpanda.skills.management_tools import SkillIdInput, SkillListInput, create_skill_management_specs
from tests.skills.test_package import write_skill


class SkillManagementToolsTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_skill_remains_observable_testable_and_diagnosable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = RedPandaHome(root / ".redpanda")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(workspace)
            await service.install_local(source)
            await service.set_enabled("demo", False)
            specs = {
                spec.name: spec
                for spec in create_skill_management_specs(service)
            }

            listed = await specs["list_installed_skills"].handler(SkillListInput(skill_id="demo"))
            tested = await specs["test_installed_skill"].handler(
                SkillIdInput(skill_id="demo")
            )

            self.assertFalse(listed["data"]["skills"][0]["enabled"])
            self.assertEqual(tested["code"], "SKILL_PACKAGE_VALID")
            self.assertFalse(tested["data"]["enabled"])

    async def test_map_and_help_use_the_registered_operation_schemas(self):
        from redpanda.skills.composition import build_skills
        from redpanda.tools.spec import ToolArgumentsError

        with tempfile.TemporaryDirectory() as directory:
            assembly = build_skills(RedPandaHome(Path(directory)))
            from redpanda.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE

            specs = {item.name: item for item in assembly.management_specs}
            specs.update({item.name: item.proposal_spec for item in assembly.control_operations})
            read_specs = {item.name: item for item in assembly.service.tool_catalog.tool_specs()}
            self.assertEqual(set(read_specs), {LOAD_SKILL, READ_SKILL_RESOURCE})
            self.assertTrue(set(read_specs).isdisjoint(specs))
            specs.update(read_specs)
            help_spec = specs["skill_help"]
            result = await help_spec.handler(help_spec.parameters.validate({}))
            entries = result["data"]["operations"]
            self.assertEqual({item["operation"] for item in entries}, {
                "help", "load", "read_resource", "list", "test", "install", "update", "set_enabled", "uninstall",
            })
            self.assertEqual({item["tool"] for item in entries}, set(specs))
            for entry in entries:
                detail = await help_spec.handler(help_spec.parameters.validate({"operation": entry["operation"]}))
                self.assertEqual(detail["data"]["tool"], specs[entry["tool"]].to_openai_tool()["function"])
                self.assertEqual(entry["requires_approval"], specs[entry["tool"]].control_boundary)
            with self.assertRaises(ToolArgumentsError):
                help_spec.parameters.validate({"operation": "repair"})
            self.assertEqual(await assembly.service.list_skills(), ())
