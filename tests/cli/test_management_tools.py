import tempfile
import unittest
from pathlib import Path

from redpanda.cli.application import CliApplicationService
from redpanda.cli.management_tools import (
    INSPECT_CLI,
    LIST_INSTALLED_CLIS,
    TEST_CLI,
    CliIdInput,
    create_cli_management_specs,
)
from redpanda.paths import RedPandaHome
from redpanda.tools.spec import EmptyInput
from tests.cli.fakes import FakeExecutor, make_result


class CliManagementToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.home = RedPandaHome(root / ".redpanda")
        self.home.initialize()
        self.executable = root / "bin" / "rg.exe"
        self.executable.parent.mkdir()
        self.executable.write_text("", encoding="utf-8")
        self.executor = FakeExecutor()
        self.executor.route("--help", make_result(stdout="Usage: rg PATTERN"))
        self.executor.route("--version", make_result(stdout="ripgrep 14.1.1"))
        self.service = CliApplicationService(self.home, self.executor)
        self.specs = {
            spec.name: spec
            for spec in create_cli_management_specs(self.service)
        }

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _install(self):
        candidate = await self.service.prepare_install(
            "rg",
            "Fast text search",
            str(self.executable),
        )
        return await self.service.install_frozen(
            candidate.name,
            candidate.description,
            candidate.source,
            candidate.resolved_path,
        )

    async def test_list_empty(self):
        result = await self.specs[LIST_INSTALLED_CLIS].handler(EmptyInput())
        self.assertEqual(result["code"], "CLIS_LISTED")
        self.assertEqual(result["data"]["clis"], [])

    async def test_list_and_inspect_registered_cli(self):
        await self._install()

        listed = await self.specs[LIST_INSTALLED_CLIS].handler(EmptyInput())
        self.assertEqual(len(listed["data"]["clis"]), 1)
        self.assertEqual(listed["data"]["clis"][0]["name"], "rg")

        inspected = await self.specs[INSPECT_CLI].handler(CliIdInput(cli_id="rg"))
        self.assertEqual(inspected["code"], "CLI_INSPECTED")
        self.assertEqual(inspected["data"]["record"]["version"], "14.1.1")

    async def test_inspect_unknown_cli(self):
        result = await self.specs[INSPECT_CLI].handler(CliIdInput(cli_id="fd"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CLI_NOT_INSTALLED")

    async def test_test_cli_returns_measured_facts(self):
        await self._install()
        self.executor.routes["--version"] = make_result(stdout="ripgrep 15.0.0")

        result = await self.specs[TEST_CLI].handler(CliIdInput(cli_id="rg"))

        self.assertEqual(result["code"], "CLI_TEST_PASSED")
        self.assertEqual(result["data"]["cli_id"], "rg")
        self.assertEqual(result["data"]["version"], "15.0.0")
        self.assertTrue(result["data"]["health"]["help_ok"])

    async def test_test_unknown_cli(self):
        result = await self.specs[TEST_CLI].handler(CliIdInput(cli_id="fd"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CLI_NOT_INSTALLED")
