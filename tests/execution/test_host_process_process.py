"""Windows owned process lifetime through the same Host port used by tools."""
import asyncio
from functools import partial
import json
import os
from pathlib import Path
import sys
import shutil
from http.client import HTTPConnection
from unittest.mock import patch

import pytest

from redpanda.assistant.host.process_port import WorkerProcessPort
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.execution.processes import ProcessManager
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome
from tests.fixtures.session_worker import ProcessLlm, config_for

pytestmark = [pytest.mark.process, pytest.mark.skipif(os.name != "nt", reason="Windows owned processes")]


def ps(*args):
    return "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in args)


async def until(predicate):
    async with asyncio.timeout(15):
        while not await predicate():
            await asyncio.sleep(.03)


def test_owned_process_output_exit_and_session_isolation(tmp_path):
    async def scenario():
        manager = ProcessManager(tmp_path / "logs", os.environ)
        try:
            entry = await manager.start("a", ps(sys.executable, "-u", "-c", "import time;print('READY');time.sleep(60)"), tmp_path)
            identity = entry["process_id"]
            async def ready():
                result = await manager.read("a", identity, 0, 0)
                return "READY" in result["stdout"]["content"]
            await until(ready)
            assert (await manager.read("a", identity, 0, 0))["state"] == "running"
            assert await manager.read("b", identity, 0, 0) is None
            assert await manager.stop("b", identity) is None
            assert await manager.list("b") == []
            stopped = await manager.stop("a", identity)
            assert stopped["state"] == "stopped"
            assert (await manager.read("a", identity, 0, 0))["stdout"]["content"].strip() == "READY"
            assert await manager.stop("a", identity) == stopped
            exited = await manager.start("a", ps(sys.executable, "-c", "import sys;sys.exit(23)") + "; exit $LASTEXITCODE", tmp_path)
            async def finished():
                return (await manager.read("a", exited["process_id"], 0, 0))["state"] == "exited"
            await until(finished)
            assert (await manager.list("a"))[-1]["exit_code"] == 23
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_descendant_outlives_shell_until_owner_stops_job(tmp_path):
    async def scenario():
        manager = ProcessManager(tmp_path / "logs", os.environ)
        try:
            code = (
                "import subprocess,sys;subprocess.Popen([sys.executable,'-u','-c',"
                "\"import time;from pathlib import Path;Path('child-ready').touch();time.sleep(60)\"],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
            )
            entry = await manager.start("a", ps(sys.executable, "-c", code), tmp_path)
            async def ready():
                from redpanda.execution.process_win import wait_object
                return (tmp_path / "child-ready").exists() and wait_object(
                    manager._processes[entry["process_id"]].running.info.process, 0,
                ) == 0
            await until(ready)
            assert (await manager.list("a"))[0]["state"] == "running"
            await manager.close()
            assert (await manager.list("a"))[0]["state"] == "stopped"
            with pytest.raises(RuntimeError, match="closed"):
                await manager.start("a", "echo x", tmp_path)
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_cancelled_start_reclaims_the_created_job(tmp_path):
    import threading
    from redpanda.execution.process_win import Running
    async def scenario():
        manager = ProcessManager(tmp_path / "logs", os.environ)
        spawned, release = threading.Event(), threading.Event()
        processes = []
        def held_spawn(*args):
            running = Running(*args)
            processes.append(running)
            spawned.set()
            assert release.wait(10)
            return running
        try:
            with patch("redpanda.execution.process_win.Running", held_spawn):
                pending = asyncio.create_task(manager.start("a", "Start-Sleep -Seconds 60", tmp_path))
                async def created():
                    return spawned.is_set()
                try:
                    await until(created)
                    pending.cancel()
                finally:
                    release.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert processes[0].job is None
            assert processes[0].info.process is None
            assert await manager.list("a") == []
        finally:
            release.set()
            await manager.close()
    asyncio.run(scenario())


def test_host_port_survives_worker_retirement_and_archive_stops_process(tmp_path):
    from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
    async def scenario():
        root = tmp_path / "project"; root.mkdir()
        home = RedPandaHome(tmp_path / "home")
        store = SessionStore(home.runtime_sessions_root)
        workspace = WorkspaceRegistry.load(home.workspaces_path).register_path(root)
        output = []
        started = []
        class Llm:
            async def chat(self, messages, model, **kwargs):
                results = [message for message in messages if message["role"] == "tool"]
                if not results:
                    response = LLMResponse(content="", calls=(ToolCall("start", "start_process", json.dumps({
                        "command": ps(sys.executable, "-u", "-c", "import time;print('READY');time.sleep(60)"),
                    })),))
                else:
                    result = json.loads(results[-1]["content"])
                    assert result["code"] == "PROCESS_STARTED", result
                    started.append(result["data"])
                    response = LLMResponse(content="done")
                return LLMCallResult(response, LLMUsage(input_tokens=10, output_tokens=5))
        host = HostSupervisor(store, partial(config_for, root), home,
                              lambda *args: output.append(args), llm=Llm())
        try:
            await host.create("a", workspace.workspace_id)
            await host.set_auto_authorize("a", True)
            await host.accept_input("a", "finish", delivery_id="one")
            async def retired():
                return bool(output) and not host.workers and not host.watchers
            await until(retired)
            entry = started[0]
            replacement_port = WorkerProcessPort(host._route, "a")
            assert (await replacement_port.list())[0]["process_id"] == entry["process_id"]
            assert (await replacement_port.read(entry["process_id"], 0, 0))["state"] == "running"
            await host.archive("a")
            assert (await replacement_port.list())[0]["state"] == "stopped"
        finally:
            await host.close()
    asyncio.run(scenario())


def test_host_command_runs_without_file_service_or_task_hook(tmp_path):
    from redpanda.assistant.assembly import build_assistant_assembly
    from redpanda.runtime import MemoryJournal
    from redpanda.runtime.dispatcher import AttemptContext
    from tests.fixtures.workspaces import workspace_record
    async def scenario():
        root = tmp_path / "project"; root.mkdir()
        assembly = await build_assistant_assembly(
            config_for(root), lambda *args: None, MemoryJournal(), session_id="a",
            workspace=workspace_record(root), home=RedPandaHome(tmp_path / "home"),
        )
        try:
            context = AttemptContext("a", "host-write", "host-write", 1)
            with patch.dict(os.environ, {"REDPANDA_FILESYSTEM_EXECUTABLE": str(tmp_path / "missing.exe")}), patch(
                "redpanda.execution.local.windows_task.native_task_launcher", return_value=tmp_path / "missing-launcher.exe",
            ):
                arguments = {"command": "[IO.File]::WriteAllText((Join-Path $PWD.Path 'value.txt'),'HOST')"}
                result = await assembly.bindings["execute_command"].handler(context, {**arguments, "file_access": "host"})
                assert result["data"]["exit_code"] == 0, result
                assert (root / "value.txt").read_text() == "HOST"
                arguments["command"] = arguments["command"].replace("'HOST'", "'MANAGED'")
                result = await assembly.bindings["execute_command"].handler(context, arguments)
                assert result["code"] == "FILESYSTEM_UNAVAILABLE", result
                assert (root / "value.txt").read_text() == "HOST"
        finally:
            await assembly.scheduler.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("launcher", ["node", "npm"])
def test_host_server_observes_multiple_managed_edits_and_restore(tmp_path, launcher):
    from redpanda.assistant.assembly import build_assistant_assembly
    from redpanda.filesystem import workspace_files, operation_id
    from redpanda.runtime import SqliteJournal
    from redpanda.runtime.dispatcher import AttemptContext
    node = shutil.which("node")
    assert node is not None, "Node.js required for development server verification"
    npm = shutil.which("npm.cmd")
    assert npm is not None, "npm required for development server verification"
    async def scenario():
        root = tmp_path / "project"; root.mkdir()
        (root / "value.txt").write_text("BASE")
        shutil.copyfile(Path(__file__).parents[1] / "fixtures" / "host_watch_server.cjs", root / "server.cjs")
        (root / "package.json").write_text(json.dumps({"scripts": {"dev": "node server.cjs"}}))
        home = RedPandaHome(tmp_path / "home")
        store = SessionStore(home.runtime_sessions_root)
        workspace = WorkspaceRegistry.load(home.workspaces_path).register_path(root)
        host = HostSupervisor(store, partial(config_for, root), home, lambda *args: None, llm=ProcessLlm(root))
        assembly = None
        try:
            await host.create("a", workspace.workspace_id)
            assembly = await build_assistant_assembly(
                config_for(root), lambda *args: None, SqliteJournal(store.require("a")), session_id="a",
                workspace=workspace, home=home, session_transport=host._route,
            )
            async def tool(name, arguments, identity):
                return await assembly.bindings[name].handler(AttemptContext("a", identity, identity, 1), arguments)
            command = ps(node, "server.cjs") if launcher == "node" else ps(npm, "run", "dev")
            result = await tool("start_process", {"command": command}, "start")
            assert result["code"] == "PROCESS_STARTED", result
            identity = result["data"]["process_id"]
            ready = []
            async def listening():
                output = await tool("read_process", {"process_id": identity}, "read")
                for line in output["data"]["stdout"]["content"].splitlines():
                    if line.startswith('{"pid":'):
                        ready.append(json.loads(line))
                        return True
                return False
            await until(listening)
            port, pid = ready[0]["port"], ready[0]["pid"]
            def request():
                connection = HTTPConnection("127.0.0.1", port, timeout=1)
                try:
                    connection.request("GET", "/")
                    return json.loads(connection.getresponse().read())
                finally:
                    connection.close()
            async def observes(value):
                async def predicate():
                    actual = await asyncio.to_thread(request)
                    assert actual["pid"] == pid
                    return actual["value"] == value
                await until(predicate)
            await observes("BASE")
            result = await tool("execute_command", {
                "command": "Start-Sleep -Seconds 10", "file_access": "host", "timeout_seconds": 1,
            }, "foreground-timeout")
            assert result["code"] == "COMMAND_TIMEOUT", result
            await observes("BASE")
            for old, new, command_id in (("BASE", "ONE1", "first"), ("ONE1", "TWO2", "second")):
                result = await tool("apply_patch", {"path": "value.txt", "old_block": old, "new_block": new}, command_id)
                assert result["ok"], result
                await observes(new)
            files = workspace_files(home, workspace)
            await files.restore(operation_id("a", "first"), identity=operation_id("a", "restore"), policy="preserve")
            await observes("ONE1")
            state = await tool("read_process", {"process_id": identity}, "read-final")
            assert state["data"]["state"] == "running"
            assert "RELOAD" in state["data"]["stdout"]["content"]
            result = await tool("stop_process", {"process_id": identity}, "stop")
            assert result["data"]["state"] == "stopped"
            with pytest.raises(OSError):
                await asyncio.to_thread(request)
        finally:
            if assembly is not None:
                await assembly.scheduler.close()
            await host.close()
    asyncio.run(scenario())
