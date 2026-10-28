"""扩展登记按数据根由所有 Agent 进程共享：并发提交互不覆盖。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time

import pytest

from konden.cli.registry import CliRegistry

pytestmark = pytest.mark.process

_WRITER = """
import asyncio, sys, time
from pathlib import Path
from konden.cli.registry import CliRegistry
from tests.cli.test_registry import make_record

root, start, prefix = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
while not start.exists():
    time.sleep(0.005)

async def main():
    registry = CliRegistry(root)
    for index in range(30):
        await registry.add(make_record(f"{prefix}-{index}"))

asyncio.run(main())
"""


def test_concurrent_processes_keep_each_others_registrations(tmp_path):
    root, start = tmp_path / "clis", tmp_path / "start"
    writers = [
        subprocess.Popen([sys.executable, "-c", _WRITER, str(root), str(start), prefix])
        for prefix in ("a", "b")
    ]
    start.touch()
    for writer in writers:
        assert writer.wait(timeout=60) == 0

    names = {record.name for record in asyncio.run(CliRegistry(root).list_clis())}
    assert names == {f"{prefix}-{index}" for prefix in ("a", "b") for index in range(30)}


_INSTALLER = """
import asyncio, json, sys
from pathlib import Path
from unittest.mock import patch
from konden.cli.application import CliApplicationService
from konden.cli.errors import CliAlreadyInstalledError
from konden.cli.models import CliSourceRef
from konden.cli.provision import ProvisionedCli
from konden.paths import KondenHome
from tests.cli.fakes import FakeExecutor, make_result

root, role = Path(sys.argv[1]), sys.argv[2]
home = KondenHome(root / "home")
home.initialize()
executor = FakeExecutor()
executor.route("--help", make_result(stdout="Usage: gh"))
executor.route("--version", make_result(stdout="gh 1.0.0"))
service = CliApplicationService(home, executor)

async def provision(name, source, *, install_root):
    (root / f"{role}.provisioning").touch()
    if role == "first":
        while not (root / "release").exists():
            await asyncio.sleep(0.01)
    directory = install_root / name / "1.0.0"
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "gh.exe"
    binary.write_text(role, encoding="utf-8")
    return ProvisionedCli(str(binary), "1.0.0", directory)

async def main():
    (root / f"{role}.ready").touch()
    with patch("konden.cli.application.provision_github_release", new=provision):
        try:
            await service.install_frozen(
                "gh", role, CliSourceRef("github-release", f"{role}/cli", "v1.0.0"), ""
            )
        except CliAlreadyInstalledError:
            result = "already-installed"
        else:
            result = "installed"
    (root / f"{role}.result").write_text(json.dumps(result), encoding="utf-8")

asyncio.run(main())
"""


def test_same_cli_installation_is_serialized_across_processes(tmp_path):
    """独立 Worker 的失败安装不能改写获胜者的文件或来源。"""
    installers = []

    def start(role):
        process = subprocess.Popen(
            [sys.executable, "-c", _INSTALLER, str(tmp_path), role],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        installers.append(process)
        marker = tmp_path / ("first.provisioning" if role == "first" else "second.ready")
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert process.poll() is None, process.communicate()[1].decode()
            assert time.monotonic() < deadline, f"{role} 没有进入安装"
            time.sleep(0.01)
        return process

    try:
        start("first")
        second = start("second")
        # 第一位仍在下载；第二位应等待，而不是先覆盖目录并完成登记。
        try:
            second.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        (tmp_path / "release").touch()
        for process in installers:
            _, error = process.communicate(timeout=15)
            assert process.returncode == 0, error.decode()
        assert json.loads((tmp_path / "first.result").read_text()) == "installed"
        assert json.loads((tmp_path / "second.result").read_text()) == "already-installed"
        records = asyncio.run(CliRegistry(tmp_path / "home" / "clis").list_clis())
        assert len(records) == 1
        assert records[0].source.locator == "first/cli"
        assert (tmp_path / "home" / "clis" / "_installs" / "gh" / "1.0.0" / "gh.exe").read_text() == "first"
    finally:
        for process in installers:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
