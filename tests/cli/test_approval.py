import tempfile
import unittest
from pathlib import Path

from redpanda.cli.approval import (
    CLI_INSTALL_ACTION,
    CLI_UNINSTALL_ACTION,
    CliInstallApprovalHandler,
    CliUninstallApprovalHandler,
    create_cli_install_proposal_spec,
    create_cli_uninstall_proposal_spec,
    CliInstallProposalInput,
    CliIdProposalInput,
)
from redpanda.cli.application import CliApplicationService
from redpanda.paths import RedPandaHome
from redpanda.tools.control import ControlApprovalProposal, ControlPreparationFailure
from tests.cli.fakes import FakeExecutor, make_result


class CliInstallApprovalTest(unittest.IsolatedAsyncioTestCase):
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
        self.propose = create_cli_install_proposal_spec(self.service)
        self.handler = CliInstallApprovalHandler(self.service)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_propose_returns_approval_request_with_frozen_identity(self):
        request = await self.propose.handler(CliInstallProposalInput(
            name="rg",
            description="Fast text search",
            locator=str(self.executable),
        ))

        self.assertIsInstance(request, ControlApprovalProposal)
        self.assertEqual(request.action, CLI_INSTALL_ACTION)
        self.assertEqual(
            set(request.payload),
            {"name", "description", "source", "resolved_path"},
        )
        self.assertEqual(request.payload["name"], "rg")
        self.assertEqual(request.payload["source"]["kind"], "manifest")
        self.assertIn("14.1.1", request.summary)
        self.assertIn("凭据", request.risk)

    async def test_propose_rejects_unknown_command(self):
        result = await self.propose.handler(CliInstallProposalInput(
            name="rg",
            description="Fast text search",
            locator="rg",
        ))
        self.assertIsInstance(result, ControlPreparationFailure)
        self.assertFalse(result.result["ok"])
        self.assertEqual(result.result["code"], "CLI_SOURCE_ERROR")

    async def test_handler_executes_registration(self):
        request = await self.propose.handler(CliInstallProposalInput(
            name="rg",
            description="Fast text search",
            locator=str(self.executable),
        ))

        execution = await self.handler.execute(request.payload)

        self.assertTrue(execution.succeeded)
        record = await self.service.inspect("rg")
        self.assertEqual(record.version, "14.1.1")

    async def test_handler_rejects_duplicate(self):
        request = await self.propose.handler(CliInstallProposalInput(
            name="rg",
            description="Fast text search",
            locator=str(self.executable),
        ))
        await self.handler.execute(request.payload)

        second = await self.handler.execute(request.payload)

        self.assertFalse(second.succeeded)


class CliUninstallApprovalTest(unittest.IsolatedAsyncioTestCase):
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
        candidate = await self.service.prepare_install(
            "rg",
            "Fast text search",
            str(self.executable),
        )
        await self.service.install_frozen(
            candidate.name,
            candidate.description,
            candidate.source,
            candidate.resolved_path,
        )
        self.propose = create_cli_uninstall_proposal_spec(self.service)
        self.handler = CliUninstallApprovalHandler(self.service)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_propose_and_execute_uninstall(self):
        request = await self.propose.handler(CliIdProposalInput(cli_id="rg"))

        self.assertIsInstance(request, ControlApprovalProposal)
        self.assertEqual(request.action, CLI_UNINSTALL_ACTION)
        self.assertEqual(
            request.payload,
            {"cli_id": "rg", "expected_revision": 1},
        )

        execution = await self.handler.execute(request.payload)

        self.assertTrue(execution.succeeded)
        self.assertIsNone(await self.service.inspect_or_none("rg"))

    async def test_execute_rejects_revision_conflict(self):
        execution = await self.handler.execute(
            {"cli_id": "rg", "expected_revision": 99}
        )

        self.assertFalse(execution.succeeded)
        self.assertIsNotNone(await self.service.inspect_or_none("rg"))

    async def test_propose_unknown_cli(self):
        result = await self.propose.handler(CliIdProposalInput(cli_id="fd"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CLI_NOT_INSTALLED")
