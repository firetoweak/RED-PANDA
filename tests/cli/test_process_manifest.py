"""process 层：用真 shell 走 manifest 登记 → 体检 → 调用闭环。

以 sys.executable（当前 Python 解释器）作为被登记的 CLI——它一定存在、
支持 --help / --version，且输出含版本串。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

from redpanda.cli.composition import build_cli
from redpanda.cli.runtime import LOAD_CLI, LoadCliInput
from redpanda.paths import RedPandaHome

pytestmark = pytest.mark.process


class ProcessManifestSliceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = RedPandaHome(Path(self.temporary.name) / ".redpanda")
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

    @unittest.skipUnless(os.name == "nt", "where.exe 解析分支")
    async def test_where_resolution_uses_fresh_path(self):
        """PATH 目录新增后，无需重启即可解析到其中的命令。"""
        bin_dir = Path(self.temporary.name) / "freshbin"
        bin_dir.mkdir()
        shim = bin_dir / "redpanda-fake-cli.cmd"
        shim.write_text(
            "@echo off\r\n"
            "if \"%1\"==\"--version\" (echo fake-cli 9.9.1 & exit /b 0)\r\n"
            "if \"%1\"==\"--help\" (echo Usage: fake-cli & exit /b 0)\r\n"
            "exit /b 1\r\n",
            encoding="utf-8",
        )
        # 模拟「安装器写入持久化 PATH」：直接写 HKCU\Environment 的 Path，
        # finally 恢复原值（原本不存在则删除新增值）。
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                original, value_type = winreg.QueryValueEx(key, "Path")
            except OSError:
                original, value_type = None, winreg.REG_EXPAND_SZ
            try:
                winreg.SetValueEx(
                    key,
                    "Path",
                    0,
                    value_type,
                    (original + ";" if original else "") + str(bin_dir),
                )
                resolved = await self.assembly.service.installer.resolve_path(
                    "redpanda-fake-cli"
                )
                self.assertEqual(Path(resolved), shim)
            finally:
                if original is None:
                    winreg.DeleteValue(key, "Path")
                else:
                    winreg.SetValueEx(key, "Path", 0, value_type, original)
