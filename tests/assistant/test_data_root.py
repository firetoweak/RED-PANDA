from __future__ import annotations

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
        holder.kill()
        holder.wait()
        holder.stdout.close()

    with hold_data_root(home):
        pass
