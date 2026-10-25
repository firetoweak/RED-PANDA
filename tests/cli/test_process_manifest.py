"""process 层：用真 shell 走 manifest 登记 → 体检 → 调用闭环。

以 sys.executable（当前 Python 解释器）作为被登记的 CLI——它一定存在、
支持 --help / --version，且输出含版本串。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pytest

from konden.cli.composition import build_cli
from konden.cli.runtime import LOAD_CLI, LoadCliInput
from konden.paths import KondenHome

pytestmark = pytest.mark.process


class ProcessManifestSliceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = KondenHome(Path(self.temporary.name) / ".konden")
        self.home.initialize()
        self.assembly = build_cli(self.home)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_register_probe_and_load_with_real_shell(self):
        service = self.assembly.service

        candidate = await service.prepare_install(
            "python",
            "Python interpreter",
            sys.executable,
        )
        self.assertEqual(candidate.resolved_path, str(Path(sys.executable).resolve()))
        self.assertTrue(candidate.probed.health.help_ok)
        self.assertTrue(candidate.probed.health.version_ok)
        self.assertIsNotNone(candidate.probed.version)

        record = await service.install_frozen(
            candidate.name,
            candidate.description,
            candidate.source,
            candidate.resolved_path,
        )
        self.assertEqual(record.version, candidate.probed.version)

        tested = await service.test_cli("python")
        self.assertTrue(tested.probed.health.help_ok)

        specs = {spec.name: spec for spec in self.assembly.tool_catalog.tool_specs()}
        loaded = await specs[LOAD_CLI].handler(LoadCliInput(cli_id="python"))
        self.assertEqual(loaded["code"], "CLI_LOADED")
        self.assertEqual(loaded["data"]["version"], record.version)
