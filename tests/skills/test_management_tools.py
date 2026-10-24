import tempfile
import unittest
from pathlib import Path

from konden.paths import KondenHome
from konden.skills.application import SkillApplicationService
from konden.skills.management_tools import SkillIdInput, SkillListInput, create_skill_management_specs
from tests.skills.test_package import write_skill


class SkillManagementToolsTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_skill_remains_observable_testable_and_diagnosable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = KondenHome(root / ".konden")
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

