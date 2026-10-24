"""扩展登记按数据根由所有 Agent 进程共享：并发提交互不覆盖。"""

from __future__ import annotations

import asyncio
import subprocess
import sys

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
