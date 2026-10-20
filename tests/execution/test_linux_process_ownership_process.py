"""Linux subreapers must own exactly one task, including detached descendants."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import textwrap

import pytest

from redpanda.execution.processes import CommandManager

pytestmark = [pytest.mark.process, pytest.mark.skipif(sys.platform != "linux", reason="Linux ownership")]


async def until(predicate):
    async with asyncio.timeout(15):
        while not await predicate():
            await asyncio.sleep(.02)


def command(code):
    return shlex.join([sys.executable, "-u", "-c", code])


def detached_code(marker):
    child = f"import os,time;from pathlib import Path;Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)"
    return f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"


def test_detached_children_are_reaped_without_killing_other_tasks(tmp_path):
    async def scenario():
        manager = CommandManager(tmp_path / "logs", os.environ)
        try:
            detached = tmp_path / "detached"
            first = await manager.execute("a", 'cmd-39', command(detached_code(detached)), tmp_path, 0)
            second = await manager.execute("b", 'cmd-40', command("import time;print('SECOND');time.sleep(60)"), tmp_path, 0)
            async def ready():
                return detached.exists() and "SECOND" in (await manager.read("b", second["command_id"], 0, 0))["stdout"]["content"]
            await until(ready)
            escaped_pid = int(detached.read_text())
            assert (await manager.read("a", first["command_id"], 0, 0))["state"] == "running"
            assert (await manager.stop("a", first["command_id"]))["state"] == "stopped"
            assert not Path("/proc", str(escaped_pid)).exists()
            assert (await manager.read("b", second["command_id"], 0, 0))["state"] == "running"
            assert "SECOND" in (await manager.read("b", second["command_id"], 0, 0))["stdout"]["content"]
            await manager.close()
            assert not Path("/proc", str(second["pid"])).exists()
        finally:
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kill_group", [False, True], ids=["host", "host-group"])
def test_host_kill_closes_control_pipes_and_reclaims_each_tree(tmp_path, kill_group):
    root = Path(__file__).resolve().parents[2]
    code = textwrap.dedent(f"""
        import asyncio,json,os,sys
        from pathlib import Path
        sys.path.insert(0,{str(root)!r})
        from redpanda.execution.processes import CommandManager
        async def main():
            manager=CommandManager(Path('logs'),os.environ)
            entries=[]
            for name in ('one','two'):
                entries.append(await manager.execute(name,name,{command(detached_code('REPLACE'))!r}.replace('REPLACE',name),Path.cwd(),0))
            Path('ready').write_text(json.dumps(entries))
            await asyncio.Event().wait()
        asyncio.run(main())
    """)
    async def scenario():
        with (tmp_path / "host.log").open("wb") as log:
            host = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=tmp_path, stdout=log, stderr=log,
                                    start_new_session=True)
            try:
                async def ready():
                    return all((tmp_path / name).exists() for name in ("ready", "one", "two"))
                await until(ready)
                entries = json.loads((tmp_path / "ready").read_text())
                pids = [entry["pid"] for entry in entries]
                pids += [int((tmp_path / name).read_text()) for name in ("one", "two")]
                if kill_group:
                    os.killpg(host.pid, signal.SIGKILL)
                else:
                    host.kill()
                await asyncio.to_thread(host.wait, 10)
                async def stopped():
                    return all(not Path("/proc", str(pid)).exists() for pid in pids)
                await until(stopped)
            finally:
                if host.poll() is None:
                    host.kill()
                await asyncio.to_thread(host.wait, 10)
    asyncio.run(scenario())
