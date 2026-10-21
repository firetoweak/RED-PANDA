"""Owned process lifetime through the same Host port used by tools."""
import asyncio
from functools import partial
import json
import os
from pathlib import Path
import sys
import shutil
import shlex
from http.client import HTTPConnection
from unittest.mock import patch

import pytest

from redpanda.assistant.host.command_port import WorkerCommandPort
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.execution.processes import CommandManager
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.paths import RedPandaHome
from tests.fixtures.session_worker import ProcessLlm, config_for

pytestmark = [pytest.mark.process, pytest.mark.skipif(sys.platform not in {"win32", "linux"}, reason="Windows or Linux owned processes")]


def shell(*args):
    if os.name != "nt":
        return shlex.join(str(arg) for arg in args)
    return "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in args)


async def until(predicate):
    async with asyncio.timeout(15):
        while not await predicate():
            await asyncio.sleep(.03)


def test_owned_process_output_exit_and_session_isolation(tmp_path):
    async def scenario():
        manager = CommandManager(tmp_path / "logs", os.environ)
        try:
            entry = await manager.execute("a", "writer", shell(sys.executable, "-u", "-c", "import time;print('READY');time.sleep(60)"), tmp_path, 0)
            identity = entry["command_id"]
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
            exited = await manager.execute("a", "exit", shell(sys.executable, "-c", "import sys;print('HEAD'+'汉'*12000+'TAIL');sys.exit(23)") + ("; exit $LASTEXITCODE" if os.name == "nt" else ""), tmp_path, 0)
            async def finished():
                return (await manager.read("a", exited["command_id"], 0, 0))["state"] == "completed"
            await until(finished)
            assert (await manager.list("a"))[-1]["exit_code"] == 23
            result = await manager.wait("a", exited["command_id"])
            assert result["stdout"]["truncated"]
            assert "HEAD" not in result["stdout"]["content"]
            assert result["stdout"]["content"].rstrip().endswith("TAIL")
            page = await manager.read("a", exited["command_id"], 0, 0)
            assert page["stdout"]["has_more"]
            following = await manager.read("a", exited["command_id"], page["stdout"]["next_offset"], 0)
            assert following["stdout"]["content"].startswith("汉")
            assert "�" not in following["stdout"]["content"]
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_descendant_outlives_shell_until_owner_stops_job(tmp_path):
    async def scenario():
        manager = CommandManager(tmp_path / "logs", os.environ)
        try:
            code = (
                "import subprocess,sys;subprocess.Popen([sys.executable,'-u','-c',"
                "\"import time;from pathlib import Path;Path('child-ready').touch();time.sleep(60)\"],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
            )
            entry = await manager.execute("a", "tree", shell(sys.executable, "-c", code), tmp_path, 0)
            async def ready():
                if os.name == "nt":
                    from redpanda.execution.process_win import wait_object
                    exited = wait_object(manager._commands[entry["command_id"]].running.info.process, 0) == 0
                else:
                    owned = manager._commands[entry["command_id"]]
                    async with owned.lock:
                        exited = await asyncio.to_thread(owned.running.poll, leader_only=True) is not None
                return (tmp_path / "child-ready").exists() and exited
            await until(ready)
            assert (await manager.list("a"))[0]["state"] == "running"
            await manager.close()
            assert (await manager.list("a"))[0]["state"] == "stopped"
            with pytest.raises(RuntimeError, match="closed"):
                await manager.execute("a", "cancelled", "echo x", tmp_path, 0)
        finally:
            await manager.close()
    asyncio.run(scenario())


def test_cancelled_worker_leaves_created_command_queryable(tmp_path):
    import threading
    if os.name == "nt":
        from redpanda.execution.process_win import Running
        module = "redpanda.execution.process_win"
    else:
        from redpanda.execution.process_linux import Running
        module = "redpanda.execution.process_linux"
    async def scenario():
        manager = CommandManager(tmp_path / "logs", os.environ)
        spawned, release = threading.Event(), threading.Event()
        processes = []
        def held_spawn(*args):
            running = Running(*args)
            processes.append(running)
            spawned.set()
            assert release.wait(10)
            return running
        try:
            with patch(module + ".Running", held_spawn):
                pending = asyncio.create_task(manager.execute("a", "cancelled", "Start-Sleep -Seconds 60" if os.name == "nt" else "sleep 60", tmp_path, 0))
                async def created():
                    return spawned.is_set()
                try:
                    await until(created)
                    pending.cancel()
                finally:
                    release.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            # A dead Worker does not revoke Host ownership or relaunch the command.
            result = await manager.lookup("a", "cancelled")
            assert result["state"] == "running"
            await manager.stop("a", result["command_id"])
            assert (await manager.lookup("a", result["command_id"]))["state"] == "stopped"
        finally:
            release.set()
            await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("completion", ["stop", "exit"])
def test_host_port_survives_worker_retirement(tmp_path, completion):
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
                    response = LLMResponse(content="", calls=(ToolCall("start", "execute_command", json.dumps({
                        "command": shell(sys.executable, "-u", "-c", "import time;from pathlib import Path;print('READY');\nwhile not Path('finish').exists(): time.sleep(.05)"), "wait_seconds": 0,
                    })),))
                else:
                    result = json.loads(results[-1]["content"])
                    assert result["code"] == "COMMAND_BACKGROUND", result
                    started.append(result["data"]["background"])
                    response = LLMResponse(content="done")
                return LLMCallResult(response, LLMUsage(input_tokens=10, output_tokens=5))
        host = HostSupervisor(store, partial(config_for, root), home,
                              lambda *args: output.append(args), llm=Llm())
        try:
            await host.create("a", workspace.workspace_id)
            await host.set_auto_authorize(True)
            await host.accept_input("a", "finish", delivery_id="one")
            async def retired():
                return bool(output) and not host.workers
            await until(retired)
            entry = started[0]
            replacement_port = WorkerCommandPort(host._route, "a")
            assert (await replacement_port.list())[0]["command_id"] == entry["command_id"]
            assert (await replacement_port.read(entry["command_id"], 0, 0))["state"] == "running"
            previous = len(output)
            await host.receive_user_message("a", "new request while command is running", delivery_id="two")
            async def responsive():
                return len(output) > previous and not host.workers
            await until(responsive)
            assert (await replacement_port.read(entry["command_id"], 0, 0))["state"] == "running"
            # Pausing the Agent does not revoke an already acknowledged background command.
            await host.cancel_turn("a")
            assert (await replacement_port.read(entry["command_id"], 0, 0))["state"] == "running"
            before = len(output)
            if completion == "stop":
                await replacement_port.stop(entry["command_id"])
            else:
                (root / "finish").touch()
            from redpanda.runtime import DomainFactCommitted, SqliteJournal
            journal = SqliteJournal(store.require("a"))
            async def notified():
                events = await journal.snapshot("a")
                return any(isinstance(event.payload, DomainFactCommitted)
                           and event.payload.fact_type == "background_command_finished" for event in events)
            await until(notified)
            assert len(output) == before
            await host.retry("a")
            async def reported():
                return len(output) > before
            await until(reported)
            await host.wait_quiescent("a")
            events = await journal.snapshot("a")
            assert len([event for event in events if isinstance(event.payload, DomainFactCommitted)
                        and event.payload.fact_type == "background_command_finished"]) == 1
        finally:
            await host.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("launcher", ["node", "npm"])
def test_host_server_observes_file_edits(tmp_path, launcher):
    from redpanda.assistant.assembly import build_assistant_assembly
    from redpanda.runtime import SqliteJournal
    from redpanda.runtime.dispatcher import AttemptContext
    node = shutil.which("node")
    assert node is not None, "Node.js required for development server verification"
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
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
            command = shell(node, "server.cjs") if launcher == "node" else shell(npm, "run", "dev")
            result = await tool("execute_command", {"command": command, "wait_seconds": 0}, "start")
            assert result["code"] == "COMMAND_BACKGROUND", result
            identity = result["data"]["background"]["command_id"]
            ready = []
            async def listening():
                output = await tool("read_command", {"command_id": identity}, "read")
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
                "command": "Start-Sleep -Seconds 10" if os.name == "nt" else "sleep 10", "wait_seconds": 1,
            }, "foreground-timeout")
            assert result["code"] == "COMMAND_BACKGROUND", result
            await observes("BASE")
            for old, new, command_id in (("BASE", "ONE1", "first"), ("ONE1", "TWO2", "second")):
                result = await tool("apply_patch", {"path": "value.txt", "old_block": old, "new_block": new}, command_id)
                assert result["ok"], result
                await observes(new)
            state = await tool("read_command", {"command_id": identity}, "read-final")
            assert state["data"]["state"] == "running"
            assert "RELOAD" in state["data"]["stdout"]["content"]
            result = await tool("stop_command", {"command_id": identity}, "stop")
            assert result["data"]["state"] == "stopped"
            with pytest.raises(OSError):
                await asyncio.to_thread(request)
        finally:
            if assembly is not None:
                await assembly.scheduler.close()
            await host.close()
    asyncio.run(scenario())
