import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from konden.cli.application import CliApplicationService
from konden.cli.errors import (
    CliAlreadyInstalledError,
    CliInputError,
    CliNotFoundError,
    CliSourceError,
)
from konden.paths import KondenHome
from tests.cli.fakes import FakeExecutor, make_result


class CliApplicationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.home = KondenHome(root / ".konden")
        self.home.initialize()
        self.executable = root / "bin" / "rg.exe"
        self.executable.parent.mkdir()
        self.executable.write_text("", encoding="utf-8")
        self.executor = FakeExecutor()
        self.executor.route(
            "--help",
            make_result(stdout="Usage: rg [OPTIONS] PATTERN\n  --json  JSON output"),
        )
        self.executor.route(
            "--version",
            make_result(stdout="ripgrep 14.1.1 (rev 1234)"),
        )
        self.service = CliApplicationService(self.home, self.executor)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _install(self) -> object:
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

    async def test_manifest_install_probes_and_registers_facts(self):
        candidate = await self.service.prepare_install(
            "rg",
            "Fast text search",
            str(self.executable),
        )
        self.assertEqual(candidate.resolved_path, str(self.executable.resolve()))
        self.assertEqual(candidate.probed.version, "14.1.1")
        self.assertTrue(candidate.probed.health.help_ok)
        self.assertTrue(candidate.probed.health.help_mentions_json)

        record = await self.service.install_frozen(
            candidate.name,
            candidate.description,
            candidate.source,
            candidate.resolved_path,
        )
        self.assertEqual(record.name, "rg")
        self.assertEqual(record.version, "14.1.1")
        self.assertEqual(record.source.kind, "manifest")
        self.assertEqual(record.revision, 1)
        self.assertTrue(record.health.help_ok)

    async def test_install_rejects_duplicate(self):
        await self._install()
        with self.assertRaises(CliAlreadyInstalledError):
            await self.service.prepare_install(
                "rg",
                "Fast text search",
                str(self.executable),
            )

    async def test_explicit_path_must_exist(self):
        with self.assertRaises(CliInputError):
            await self.service.prepare_install(
                "rg",
                "Fast text search",
                str(Path(self.temporary.name) / "missing.exe"),
            )

    async def test_command_name_not_on_path_is_source_error(self):
        with self.assertRaises(CliSourceError):
            await self.service.prepare_install(
                "rg", "Fast text search", "konden-missing-cli"
            )

    async def test_install_frozen_rejects_vanished_candidate_path(self):
        candidate = await self.service.prepare_install(
            "rg",
            "Fast text search",
            str(self.executable),
        )
        self.executable.unlink()
        with self.assertRaises(CliInputError):
            await self.service.install_frozen(
                candidate.name,
                candidate.description,
                candidate.source,
                candidate.resolved_path,
            )

    async def test_refresh_updates_version_and_health(self):
        record = await self._install()
        self.executor.routes["--version"] = make_result(stdout="ripgrep 15.0.0")

        refreshed = await self.service.refresh(
            "rg",
            expected_revision=record.revision,
        )

        self.assertEqual(refreshed.version, "15.0.0")
        self.assertEqual(refreshed.revision, 2)
        self.assertEqual(refreshed.resolved_path, record.resolved_path)

    async def test_refresh_rejects_vanished_path(self):
        record = await self._install()
        self.executable.unlink()
        with self.assertRaises(CliInputError):
            await self.service.refresh("rg", expected_revision=record.revision)

    async def test_repair_resolves_path_again(self):
        def place_rg(dirname: str) -> Path:
            target = Path(self.temporary.name) / dirname / (
                "rg.exe" if os.name == "nt" else "rg"
            )
            target.parent.mkdir()
            target.write_text("", encoding="utf-8")
            target.chmod(0o755)
            return target

        first = place_rg("path1")
        with patch(
            "konden.cli.installer.latest_persistent_path",
            return_value=str(first.parent),
        ):
            candidate = await self.service.prepare_install(
                "rg",
                "Fast text search",
                "rg",
            )
        record = await self.service.install_frozen(
            candidate.name,
            candidate.description,
            candidate.source,
            candidate.resolved_path,
        )
        moved = place_rg("path2")
        with patch(
            "konden.cli.installer.latest_persistent_path",
            return_value=str(moved.parent),
        ):
            repaired = await self.service.repair(
                "rg",
                expected_revision=record.revision,
            )

        self.assertTrue(os.path.samefile(repaired.resolved_path, moved))
        self.assertEqual(repaired.revision, 2)

    async def test_uninstall_removes_registration_only(self):
        record = await self._install()
        removed = await self.service.uninstall(
            "rg",
            expected_revision=record.revision,
        )
        self.assertEqual(removed.name, "rg")
        self.assertTrue(self.executable.is_file())
        with self.assertRaises(CliNotFoundError):
            await self.service.inspect("rg")

    async def test_revision_conflict_is_rejected(self):
        await self._install()
        with self.assertRaises(CliInputError):
            await self.service.uninstall("rg", expected_revision=99)

    async def test_test_cli_returns_measured_facts_without_writing_back(self):
        record = await self._install()
        self.executor.routes["--version"] = make_result(stdout="ripgrep 15.0.0")

        result = await self.service.test_cli("rg")

        self.assertEqual(result.probed.version, "15.0.0")
        self.assertTrue(result.probed.health.help_ok)
        unchanged = await self.service.inspect("rg")
        self.assertEqual(unchanged.version, "14.1.1")
        self.assertEqual(unchanged.revision, record.revision)

    async def test_test_cli_with_vanished_executable_is_not_found(self):
        await self._install()
        self.executable.unlink()
        with self.assertRaises(CliNotFoundError):
            await self.service.test_cli("rg")
