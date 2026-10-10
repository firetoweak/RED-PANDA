"""Real assembly, native file tools, shell, and reversible model restoration."""
import asyncio
from collections import deque
import json
import os
import shlex
import sys

import pytest

from redpanda.assistant.assembly import build_assistant_assembly
from redpanda.assistant.workspace_versions import project_workspace_versions, workspace_version_event, WORKSPACE_RESCUE_FACT
from redpanda.assistant.host.session_store import SessionStore
from redpanda.sandbox.child_files import workspace_versions
from redpanda.config import AssistantConfig
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from redpanda.paths import RedPandaHome
from redpanda.sandbox.versions import native_executable
from redpanda.runtime import CommandOutcomeReceived, DomainFactCommitted, InvokeTool, MemoryJournal, SqliteJournal, StepCommitted
from redpanda.runtime.dispatcher import AttemptContext
from tests.fixtures.workspaces import workspace_record
from tests.session_scheduler import SettlingScheduler

pytestmark = [pytest.mark.process, pytest.mark.skipif(
    not native_executable().is_file(),
    reason="需要已构建的原生 sandbox",
)]


class CodingModel:
    def __init__(self):
        self.actions = deque()

    async def chat(self, messages, model, *, tools=None):
        action = self.actions.popleft()
        calls = () if action is None else (ToolCall("provider-call", action[0], json.dumps(action[1])),)
        return LLMCallResult(LLMResponse(content="done", calls=calls), LLMUsage(1, 1))


def test_read_only_shell_commands_overlap_without_native_operations(tmp_path, monkeypatch):
    monkeypatch.setenv("REDPANDA_SANDBOX_EXECUTABLE", str(tmp_path / "absent-sandbox"))
    async def scenario():
        root = tmp_path / "project"
        root.mkdir()
        (root / "value").write_text("published")
        home = RedPandaHome(tmp_path / "home")
        workspace = workspace_record(root)
        view = workspace_versions(home, workspace)
        script = tmp_path / "rendezvous.py"
        # The rendezvous writes only test controls outside the workspace.
        script.write_text(
            "import json, sys, time\n"
            "from pathlib import Path\n"
            "mine, other = map(Path, sys.argv[1:])\n"
            "mine.touch()\n"
            "deadline = time.monotonic() + 10\n"
            "while not other.exists():\n"
            "    if time.monotonic() > deadline: raise TimeoutError('commands did not overlap')\n"
            "    time.sleep(0.01)\n"
            "print(json.dumps({'cwd': str(Path.cwd()), 'value': Path('value').read_text()}))\n"
        )
        def command(*arguments):
            argv = [sys.executable, "-B", str(script), *map(str, arguments)]
            if os.name == "nt":
                return "& " + " ".join("'" + item.replace("'", "''") + "'" for item in argv)
            return shlex.join(argv)
        assembly = await build_assistant_assembly(
            AssistantConfig("test", 200000, CodingModel()), lambda *args: None,
            MemoryJournal(), session_id="reads", workspace=workspace, home=home,
            scheduler_factory=SettlingScheduler,
        )
        try:
            queries = [
                assembly.bindings["execute_command"].handler(
                    AttemptContext("reads", f"read-{index}", f"attempt-{index}", 1),
                    {"command": command(tmp_path / f"ready-{index}", tmp_path / f"ready-{1-index}"),
                     "workspace_effect": "read_only", "timeout_seconds": 20},
                )
                for index in range(2)
            ]
            results = await asyncio.wait_for(asyncio.gather(*queries), 30)
            for result in results:
                assert result["ok"] and result["data"]["exit_code"] == 0, result
                output = json.loads(result["data"]["stdout"]["content"])
                assert output == {"cwd": str(root), "value": "published"}
            assert not (view.storage / "commands").exists()
            assert not (view.storage / "HEAD").exists()
        finally:
            await assembly.scheduler.close()
    asyncio.run(scenario())


def test_native_coding_and_model_restore_are_one_history(tmp_path):
    async def scenario():
        root = tmp_path / "project"
        root.mkdir()
        original = "def add(a, b):\n    return a - b\n"
        (root / "maths.py").write_text(original)
        llm = CodingModel()
        assembly = await build_assistant_assembly(AssistantConfig("test", 200000, llm),
            lambda *args: None, MemoryJournal(), session_id="coding",
            workspace=workspace_record(root), home=RedPandaHome(tmp_path / "home"), scheduler_factory=SettlingScheduler)
        await assembly.sessions.apply_auto_authorize("coding", enabled=True)
        async def run(*actions):
            llm.actions.extend((*actions, None))
            await assembly.runtime.receive_user_message("coding", "continue", delivery_id=str(len(await assembly.runtime.snapshot("coding"))))
            await assembly.scheduler.wake("coding")
            await assembly.scheduler.join()
            events = await assembly.runtime.snapshot("coding")
            tool_ids = {c.command_id for e in events if isinstance(e.payload, StepCommitted) for c in e.payload.step.commands if isinstance(c.effect, InvokeTool) and c.effect.name in {"read_file", "apply_patch", "write_file", "execute_command", "restore_workspace"}}
            outcomes = [e.payload for e in events if isinstance(e.payload, CommandOutcomeReceived) and e.payload.command_id in tool_ids]
            results = [outcome.outcome.value for outcome in outcomes]
            assert all(result["ok"] for result in results), results
            return events
        try:
            events = await run(
                ("read_file", {"path": "maths.py"}),
                ("apply_patch", {"path": "maths.py", "old_block": "return a - b", "new_block": "return a + b"}),
                ("write_file", {"path": "test_maths.py", "content": "import unittest\nfrom maths import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"}),
                ("execute_command", {"command": f"& '{sys.executable}' -B -m unittest test_maths" if os.name == "nt" else f"{shlex.quote(sys.executable)} -B -m unittest test_maths", "workspace_effect": "read_only"}),
            )
            assert "return a + b" in (root / "maths.py").read_text()
            commands = [command for e in events if isinstance(e.payload, StepCommitted) for command in e.payload.step.commands if isinstance(command.effect, InvokeTool)]
            patch_command = next(c.command_id for c in commands if c.effect.name == "apply_patch")
            events = await run(("restore_workspace", {"tool_call_id": patch_command, "policy": "original"}))
            assert (root / "maths.py").read_text() == original
            assert not (root / "test_maths.py").exists()
            restored = next(c.command_id for e in reversed(events) if isinstance(e.payload, StepCommitted) for c in e.payload.step.commands if isinstance(c.effect, InvokeTool) and c.effect.name == "restore_workspace")
            await run(("restore_workspace", {"tool_call_id": restored, "policy": "original"}))
            assert "return a + b" in (root / "maths.py").read_text()
            assert (root / "test_maths.py").is_file()
            assert all(f.version is not None for f in project_workspace_versions(await assembly.runtime.snapshot("coding")))
        finally:
            await assembly.scheduler.close()
    asyncio.run(scenario())


def test_time_travel_then_coding_uses_settled_branch_files(tmp_path):
    async def scenario():
        root = tmp_path / "project"
        root.mkdir()
        (root / "value").write_text("A")
        workspace = workspace_record(root)
        home = RedPandaHome(tmp_path / "home")
        store = SessionStore(home.sessions_root)
        await store.create("source", workspace_id=workspace.workspace_id)
        async def build(identity):
            model = CodingModel()
            assembly = await build_assistant_assembly(AssistantConfig("test", 200000, model),
                lambda *args: None, SqliteJournal(store.require(identity)), session_id=identity,
                workspace=workspace, home=home, scheduler_factory=SettlingScheduler)
            await assembly.sessions.apply_auto_authorize(identity, enabled=True)
            return assembly, model
        async def run(assembly, model, identity, name, arguments):
            model.actions.extend(((name, arguments), None))
            await assembly.runtime.receive_user_message(identity, "continue", delivery_id=str(len(await assembly.runtime.snapshot(identity))))
            await assembly.scheduler.wake(identity)
            await assembly.scheduler.join()
            return await assembly.runtime.snapshot(identity)
        source, model = await build("source")
        try:
            first = await run(source, model, "source", "apply_patch", {"path": "value", "old_block": "A", "new_block": "X"})
            first_step = next(e.payload.step.step_id for e in first if isinstance(e.payload, StepCommitted) and any(c.effect.name == "apply_patch" for c in e.payload.step.commands))
            await run(source, model, "source", "apply_patch", {"path": "value", "old_block": "X", "new_block": "Y"})
            (root / "value").write_text("H")
            marker = workspace_version_event(first, first_step)
            await store.fork_after_event("source", marker.event_id, "branch")
        finally:
            await source.scheduler.close()
        branch, model = await build("branch")
        try:
            before = await branch.runtime.snapshot("branch")
            await branch.sessions.settle_forked_workspace("branch", restore=True, delivery_id="travel")
            settled = await branch.runtime.snapshot("branch")
            assert (root / "value").read_text() == "H"
            assert all(isinstance(e.payload, DomainFactCommitted) for e in settled[len(before):])
            assert any(e.payload.fact_type == WORKSPACE_RESCUE_FACT for e in settled[len(before):])
            events = await run(branch, model, "branch", "apply_patch", {"path": "value", "old_block": "H", "new_block": "Z"})
            patched = next(c.command_id for e in reversed(events) if isinstance(e.payload, StepCommitted) for c in e.payload.step.commands if c.effect.name == "apply_patch")
            await run(branch, model, "branch", "restore_workspace", {"tool_call_id": patched, "policy": "original"})
            assert (root / "value").read_text() == "H"
        finally:
            await branch.scheduler.close()
    asyncio.run(scenario())
