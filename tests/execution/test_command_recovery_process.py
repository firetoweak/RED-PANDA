"""Worker loss must query the surviving owner, never relaunch a Command."""
import asyncio
from functools import partial
import json
import sys

import pytest

from konden.assistant.host.session_store import SessionStore
from konden.assistant.host.supervisor import HostSupervisor
from konden.filesystem.registry import WorkspaceRegistry
from konden.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from konden.paths import KondenHome
from tests.fixtures.session_worker import config_for
from tests.execution.test_host_process_process import shell, until

pytestmark = [pytest.mark.process, pytest.mark.skipif(sys.platform not in {"win32", "linux"}, reason="Host ownership")]


@pytest.mark.parametrize("at_recovery", ["running", "completed", "not_started"])
def test_worker_loss_recovers_actual_command_without_reexecution(tmp_path, at_recovery):
    async def scenario():
        root = tmp_path / "project"
        root.mkdir()
        home = KondenHome(tmp_path / "home")
        store = SessionStore(home.runtime_sessions_root)
        workspace = WorkspaceRegistry.load(home.workspaces_path).register_path(root)
        results = []
        output = []

        class Llm:
            async def chat(self, messages, model, **kwargs):
                tools = [message for message in messages if message["role"] == "tool"]
                if tools:
                    results.append(json.loads(tools[-1]["content"]))
                    response = LLMResponse(content="recovered")
                else:
                    code = ("import time;from pathlib import Path;"
                            "Path('runs').write_text(Path('runs').read_text()+'run\\n' if Path('runs').exists() else 'run\\n');"
                            "\nwhile not Path('finish').exists(): time.sleep(.02)")
                    response = LLMResponse(calls=(ToolCall("command", "execute_command", json.dumps({
                        "command": shell(sys.executable, "-u", "-c", code), "wait_seconds": 30,
                    })),))
                return LLMCallResult(response, LLMUsage(input_tokens=10, output_tokens=5))

        host = HostSupervisor(store, partial(config_for, root), home, lambda *args: output.append(args), llm=Llm())
        blocked = asyncio.Event()
        release = asyncio.Event()
        execute = host.commands.manager.execute

        async def before_spawn(*args, **kwargs):
            blocked.set()
            await release.wait()
            return await execute(*args, **kwargs)

        if at_recovery == "not_started":
            host.commands.manager.execute = before_spawn
        try:
            await host.create("a", workspace.workspace_id)
            await host.set_auto_authorize(True)
            await host.accept_input("a", "run", delivery_id="one")

            async def started():
                return blocked.is_set() if at_recovery == "not_started" else (root / "runs").exists()

            await until(started)
            worker = host.workers["a"]
            worker.process.terminate()
            await asyncio.wait_for(worker.exited.wait(), 15)
            host.commands.manager.execute = execute
            if at_recovery == "completed":
                (root / "finish").touch()
                (entry,) = host.commands.manager._commands.values()
                await host.commands.manager.wait("a", entry.command_id)
            await host.resume("a")

            async def recovered():
                return bool(output)

            await until(recovered)
            expected = {"running": "COMMAND_BACKGROUND", "completed": "COMMAND_COMPLETED",
                        "not_started": "COMMAND_NOT_STARTED"}[at_recovery]
            assert results[-1]["code"] == expected, results
            if at_recovery == "not_started":
                assert not (root / "runs").exists()
            else:
                assert (root / "runs").read_text().splitlines() == ["run"]
        finally:
            await host.close()

    asyncio.run(scenario())


def test_foreground_cancel_stops_writer_and_does_not_continue(tmp_path):
    async def scenario():
        root = tmp_path / "project"
        root.mkdir()
        home = KondenHome(tmp_path / "home")
        store = SessionStore(home.runtime_sessions_root)
        workspace = WorkspaceRegistry.load(home.workspaces_path).register_path(root)
        output = []

        class Llm:
            async def chat(self, messages, model, **kwargs):
                code = "import time;from pathlib import Path;\nwhile True:\n with Path('writes').open('a') as f: f.write('x')\n time.sleep(.02)"
                return LLMCallResult(LLMResponse(calls=(ToolCall("write", "execute_command", json.dumps({
                    "command": shell(sys.executable, "-u", "-c", code), "wait_seconds": 30,
                })),)), LLMUsage(input_tokens=10, output_tokens=5))

        host = HostSupervisor(store, partial(config_for, root), home, lambda *args: output.append(args), llm=Llm())
        try:
            await host.create("a", workspace.workspace_id)
            await host.set_auto_authorize(True)
            await host.accept_input("a", "write", delivery_id="one")

            async def started():
                return (root / "writes").exists()

            await until(started)
            await asyncio.wait_for(host.cancel_turn("a"), 15)
            captured = (root / "writes").read_bytes()
            await asyncio.sleep(.15)
            assert (root / "writes").read_bytes() == captured
            assert not output
            from konden.runtime import CommandOutcomeReceived, SqliteJournal
            events = await SqliteJournal(store.require("a")).snapshot("a")
            outcomes = [event.payload.outcome.value for event in events if isinstance(event.payload, CommandOutcomeReceived)]
            assert outcomes[-1]["code"] == "COMMAND_INTERRUPTED"
            assert not await host.commands.manager.list("a")
        finally:
            await host.close()

    asyncio.run(scenario())
