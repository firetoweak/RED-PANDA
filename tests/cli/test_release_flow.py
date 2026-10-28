"""github-release 源的接线测试：propose 钉 tag、install 失败返回、repair、uninstall。

网络与下载全部用 mock 短路，这里只验证接线与失败面。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from konden.cli.application import CliApplicationService
from konden.cli.approval import (
    CliInstallApprovalHandler,
    CliInstallProposalInput,
    CliIdProposalInput,
    CliUninstallApprovalHandler,
    create_cli_install_proposal_spec,
    create_cli_repair_proposal_spec,
    create_cli_uninstall_proposal_spec,
)
from konden.cli.errors import CliAlreadyInstalledError, CliSourceError
from konden.cli.models import CliSourceRef
from konden.cli.provision import ProvisionedCli
from konden.paths import KondenHome
from tests.cli.fakes import FakeExecutor, make_result


class ReleaseFlowTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # macOS 的 /var/folders 是指向 /private/var 的软链，路径比较统一用解析后的形式
        self.root = Path(self.temporary.name).resolve()
        self.home = KondenHome(self.root / ".konden")
        self.home.initialize()
        self.executor = FakeExecutor()
        self.executor.route(
            "--help",
            make_result(stdout="Usage: gh [COMMAND]\n  --json  Output JSON"),
        )
        self.executor.route("--version", make_result(stdout="gh version 1.2.3"))
        self.service = CliApplicationService(self.home, self.executor)
        self.installed = self.root / "installed" / "gh.exe"
        self.installed.parent.mkdir(parents=True)
        self.installed.write_text("", encoding="utf-8")

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def _provisioned(self) -> ProvisionedCli:
        return ProvisionedCli(
            resolved_path=str(self.installed),
            version="1.2.3",
            install_root=self.installed.parent,
        )

    async def _install_release(self, name: str = "gh") -> object:
        with patch(
            "konden.cli.application.provision_github_release",
            new=AsyncMock(return_value=self._provisioned()),
        ):
            return await self.service.install_frozen(
                name,
                "GitHub CLI",
                CliSourceRef("github-release", "cli/cli", "v1.2.3"),
                "",
            )


class PrepareReleaseInstallTest(ReleaseFlowTestBase):
    async def test_pins_concrete_tag_and_skips_download(self) -> None:
        resolve = AsyncMock(return_value="v9.9.9")
        provision = AsyncMock()
        with patch("konden.cli.application.resolve_release_tag", new=resolve), patch(
            "konden.cli.application.provision_github_release", new=provision
        ):
            candidate = await self.service.prepare_release_install(
                "gh", "GitHub CLI", "cli/cli"
            )
        self.assertEqual(candidate.source.kind, "github-release")
        self.assertEqual(candidate.source.requested_version, "v9.9.9")
        self.assertEqual(candidate.resolved_path, "")
        self.assertIsNone(candidate.probed)
        provision.assert_not_awaited()

    async def test_explicit_version_is_still_resolved_to_tag(self) -> None:
        resolve = AsyncMock(return_value="v1.0.0")
        with patch("konden.cli.application.resolve_release_tag", new=resolve):
            candidate = await self.service.prepare_release_install(
                "gh", "GitHub CLI", "cli/cli", "1.0.0"
            )
        self.assertEqual(candidate.source.requested_version, "v1.0.0")
        self.assertEqual(resolve.await_args.args[0].requested_version, "1.0.0")

    async def test_rejects_already_registered_name(self) -> None:
        await self._install_release()
        with patch("konden.cli.application.resolve_release_tag", new=AsyncMock()):
            with self.assertRaises(CliAlreadyInstalledError):
                await self.service.prepare_release_install(
                    "gh", "GitHub CLI", "cli/cli"
                )

    async def test_resolution_failure_propagates(self) -> None:
        with patch(
            "konden.cli.application.resolve_release_tag",
            new=AsyncMock(side_effect=CliSourceError("release 取不到")),
        ):
            with self.assertRaises(CliSourceError):
                await self.service.prepare_release_install(
                    "gh", "GitHub CLI", "cli/cli"
                )


class InstallFrozenTest(ReleaseFlowTestBase):
    async def test_shared_home_serializes_installation_before_touching_files(self) -> None:
        """失败的同名安装不得覆盖另一个 Bot 已登记的程序。"""
        other = CliApplicationService(self.home, self.executor)
        first_started, finish_first, second_started = (
            asyncio.Event(), asyncio.Event(), asyncio.Event()
        )
        binary = self.service.clis_root / "_installs" / "gh" / "1.2.3" / "gh.exe"

        async def provision(name, source, *, install_root):
            if source.locator == "first/cli":
                first_started.set()
                await finish_first.wait()
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text(source.locator, encoding="utf-8")
            return ProvisionedCli(str(binary), "1.2.3", binary.parent)

        async def install_second():
            second_started.set()
            return await other.install_frozen(
                "gh", "Second", CliSourceRef("github-release", "second/cli", "v1.2.3"), ""
            )

        with patch("konden.cli.application.provision_github_release", new=provision):
            first = asyncio.create_task(self.service.install_frozen(
                "gh", "First", CliSourceRef("github-release", "first/cli", "v1.2.3"), ""
            ))
            await asyncio.wait_for(first_started.wait(), timeout=5)
            second = asyncio.create_task(install_second())
            await second_started.wait()
            finish_first.set()
            installed = await first
            with self.assertRaises(CliAlreadyInstalledError):
                await second
        self.assertEqual(await self.service.inspect("gh"), installed)
        self.assertEqual(binary.read_text(encoding="utf-8"), "first/cli")

    async def test_registers_provisioned_path_and_source(self) -> None:
        record = await self._install_release()
        self.assertEqual(record.name, "gh")
        self.assertEqual(record.source.kind, "github-release")
        self.assertEqual(record.source.locator, "cli/cli")
        self.assertEqual(record.source.requested_version, "v1.2.3")
        self.assertEqual(record.resolved_path, str(self.installed))
        self.assertIsNotNone(record.health)
        stored = await self.service.inspect("gh")
        self.assertEqual(stored.resolved_path, str(self.installed))

    async def test_download_failure_leaves_registry_untouched(self) -> None:
        with patch(
            "konden.cli.application.provision_github_release",
            new=AsyncMock(side_effect=CliSourceError("校验失败")),
        ):
            with self.assertRaises(CliSourceError):
                await self.service.install_frozen(
                    "gh",
                    "GitHub CLI",
                    CliSourceRef("github-release", "cli/cli", "v1.2.3"),
                    "",
                )
        self.assertIsNone(await self.service.inspect_or_none("gh"))


class RepairTest(ReleaseFlowTestBase):
    async def test_release_repair_redownloads_same_tag(self) -> None:
        record = await self._install_release()
        proposal = await create_cli_repair_proposal_spec(self.service).handler(
            CliIdProposalInput(cli_id="gh")
        )
        self.assertIn("重新下载 v1.2.3", proposal.summary)
        self.assertIn("替换", proposal.risk)
        provision = AsyncMock(return_value=self._provisioned())
        with patch("konden.cli.application.provision_github_release", new=provision):
            repaired = await self.service.repair("gh", expected_revision=record.revision)
        provision.assert_awaited_once()
        self.assertEqual(
            provision.await_args.args[1].requested_version, "v1.2.3"
        )
        self.assertEqual(repaired.resolved_path, str(self.installed))

    async def test_manifest_repair_keeps_resolving_path(self) -> None:
        executable = self.root / "bin" / "rg.exe"
        executable.parent.mkdir()
        executable.write_text("", encoding="utf-8")
        candidate = await self.service.prepare_install(
            "rg", "Fast search", str(executable)
        )
        record = await self.service.install_frozen(
            candidate.name,
            candidate.description,
            candidate.source,
            candidate.resolved_path,
        )
        with patch("konden.cli.application.provision_github_release") as provision:
            repaired = await self.service.repair("rg", expected_revision=record.revision)
        provision.assert_not_called()
        self.assertEqual(repaired.resolved_path, str(executable))


class UninstallTest(ReleaseFlowTestBase):
    async def test_release_uninstall_removes_install_dir(self) -> None:
        install_dir = self.service.clis_root / "_installs" / "gh" / "1.2.3"
        install_dir.mkdir(parents=True)
        binary = install_dir / "bin" / "gh.exe"
        binary.parent.mkdir()
        binary.write_text("", encoding="utf-8")
        with patch(
            "konden.cli.application.provision_github_release",
            new=AsyncMock(
                return_value=ProvisionedCli(
                    resolved_path=str(binary),
                    version="1.2.3",
                    install_root=install_dir,
                )
            ),
        ):
            record = await self.service.install_frozen(
                "gh",
                "GitHub CLI",
                CliSourceRef("github-release", "cli/cli", "v1.2.3"),
                "",
            )
        proposal = await create_cli_uninstall_proposal_spec(self.service).handler(
            CliIdProposalInput(cli_id=record.name)
        )
        self.assertIn("卸载", proposal.summary)
        self.assertIn("删除", proposal.risk)
        self.assertIn("安装目录", proposal.risk)
        execution = await CliUninstallApprovalHandler(self.service).execute(proposal.payload)
        self.assertTrue(execution.succeeded)
        self.assertFalse((self.service.clis_root / "_installs" / "gh").exists())
        self.assertIsNone(await self.service.inspect_or_none("gh"))

    async def test_manifest_uninstall_keeps_binary(self) -> None:
        executable = self.root / "bin" / "rg.exe"
        executable.parent.mkdir()
        executable.write_text("", encoding="utf-8")
        candidate = await self.service.prepare_install(
            "rg", "Fast search", str(executable)
        )
        record = await self.service.install_frozen(
            candidate.name,
            candidate.description,
            candidate.source,
            candidate.resolved_path,
        )
        await self.service.uninstall("rg", expected_revision=record.revision)
        self.assertTrue(executable.is_file())


class InstallApprovalFailureTest(ReleaseFlowTestBase):
    async def test_download_failure_surfaces_as_failed_execution(self) -> None:
        propose = create_cli_install_proposal_spec(self.service)
        handler = CliInstallApprovalHandler(self.service)
        with patch(
            "konden.cli.application.resolve_release_tag",
            new=AsyncMock(return_value="v1.2.3"),
        ):
            request = await propose.handler(
                CliInstallProposalInput(
                    name="gh", description="GitHub CLI", repo="cli/cli"
                )
            )
        with patch(
            "konden.cli.application.provision_github_release",
            new=AsyncMock(side_effect=CliSourceError("下载归档失败：ReadTimeout")),
        ):
            execution = await handler.execute(request.payload)
        self.assertIn("v1.2.3", request.summary)
        self.assertFalse(execution.succeeded)
        self.assertIn("ReadTimeout", execution.message)
        self.assertIsNone(await self.service.inspect_or_none("gh"))

    async def test_proposal_requires_exactly_one_of_locator_and_repo(self) -> None:
        propose = create_cli_install_proposal_spec(self.service)
        for payload in (
            CliInstallProposalInput(name="gh", description="GitHub CLI"),
            CliInstallProposalInput(
                name="gh",
                description="GitHub CLI",
                locator=str(self.installed),
                repo="cli/cli",
            ),
        ):
            result = await propose.handler(payload)
            self.assertIsInstance(result, dict)
            self.assertEqual(result["code"], "CLI_INVALID")
            self.assertIn("必须且只能给一个", result["error"])
