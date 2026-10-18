from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from redpanda.assistant.host.data_root import DataRootInUse, hold_data_root
from redpanda.paths import RedPandaHome


pytestmark = pytest.mark.process

_HOLDER = """
import os, sys, time
from pathlib import Path
from redpanda.assistant.host.data_root import hold_data_root
from redpanda.paths import RedPandaHome
with hold_data_root(RedPandaHome(Path(sys.argv[1]))):
    print(os.getpid(), flush=True)
    time.sleep(120)
"""


def test_second_host_is_refused_until_the_holder_dies(tmp_path):
    home = RedPandaHome(tmp_path / "home")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(home.root)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        pid = holder.stdout.readline().strip()
        with pytest.raises(DataRootInUse, match=f"进程：{pid}\n"):
            with hold_data_root(home):
                pass
    finally:
        # venv 的 python.exe 只是启动器。kill() 停掉它之后，真正持锁的子进程还在。
        if os.name == "nt":
            subprocess.run(
                [str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                 "/PID", str(holder.pid), "/T", "/F"],
                capture_output=True,
            )
        else:
            holder.kill()
        holder.wait(timeout=10)
        holder.stdout.close()

    with hold_data_root(home):
        pass
