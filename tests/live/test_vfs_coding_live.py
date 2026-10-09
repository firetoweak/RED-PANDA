"""A real configured model codes through Worker/VFS, then Web's time travel path."""
import asyncio
import json
import os
import shutil
import sys
import time

import pytest

from redpanda.assistant.workspace_versions import WORKSPACE_RESCUE_FACT, project_workspace_versions
from redpanda.bootstrap import bootstrap_assistant
from redpanda.config import load_app_config
from redpanda.paths import RedPandaHome
from redpanda.sandbox.versions import native_executable
from redpanda.runtime import CommandOutcomeReceived, DomainFactCommitted, InvokeTool, SqliteJournal, StepCommitted

pytestmark = [pytest.mark.live, pytest.mark.skipif(
    os.environ.get("REDPANDA_RUN_LIVE_TESTS") != "1" or os.name != "nt" or not native_executable().is_file(),
    reason="需启用真实模型测试与 Windows VFS",
)]


def test_real_model_codes_and_time_travel_restores_only_assistant_changes(tmp_path, monkeypatch):
    config = load_app_config()
    connections = RedPandaHome.default().connections_path
    home = tmp_path / "home"
    home.mkdir()
    shutil.copyfile(connections, home / "connections.json")
    monkeypatch.setenv("REDPANDA_HOME", str(home))
    root = tmp_path / "project"
    root.mkdir()
    original = "def add(a, b):\n    return a - b\n"
    (root / "calc.py").write_text(original)
    (root / "test_calc.py").write_text("import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    (root / ".gitignore").write_text("ignored.txt\n")
    async def scenario():
        delivered = asyncio.Event()
        timings = {}
        progress = []
        failures = []
        def sink(session_id, output_id, text):
            if "VFS_CODING_DONE" in text:
                delivered.set()
        def tool_progress(session_id, phase, command_id, name, data):
            progress.append((phase, name))
            if phase == "start": timings.setdefault("first_tool_ms", (time.monotonic()-started)*1000)
            if phase == "finish": print("tool completed:", name, flush=True)
        async with bootstrap_assistant(sink, app_config=config, workspace_path=root,
                tool_progress_sink=tool_progress,
                session_failed_sink=lambda sid, text: failures.append(text)) as app:
            host = app.sessions
            await host.create("coding", app.workspace.workspace_id)
            selected_model = os.environ.get("REDPANDA_VFS_LIVE_MODEL", config.default_model)
            host.set_model("coding", selected_model)
            await host.select("test-browser", "coding")
            await host.set_auto_authorize("coding", True)
            command = f"[IO.File]::WriteAllText('ignored.txt', 'command-produced'); & '{sys.executable}' -B -m unittest test_calc"
            prompt = ("完成一次真实编码测试。先单独 read_file 读取 calc.py，再用 apply_patch 修复 add 的减法错误为加法。"
                      "不要修改测试文件。然后 execute_command 实际执行以下 PowerShell 命令，workspace_effect=may_write："
                      + command + "。核对测试成功，再用 read_file 核对 ignored.txt 内容。"
                      "只允许修改 calc.py、ignored.txt，勿委派任务、勿安装依赖、勿调用其他管理能力。最后回复 VFS_CODING_DONE。")
            started = time.monotonic()
            failure = asyncio.create_task(host.wait_failure())
            waiting = asyncio.create_task(delivered.wait())
            try:
                await host.receive_user_message("coding", prompt, delivery_id="input")
                async with asyncio.timeout(180):
                    done, _ = await asyncio.wait((failure, waiting), return_when=asyncio.FIRST_COMPLETED)
                    if failure in done: raise failure.result()
                    await host.wait_quiescent("coding")
                timings["coding_seconds"] = time.monotonic() - started
                assert not failures, failures
                assert "return a + b" in (root / "calc.py").read_text()
                assert (root / "ignored.txt").read_text() == "command-produced"
                events = await SqliteJournal(host.store.require("coding")).snapshot("coding")
                calls = {c.command_id: c.effect for e in events if isinstance(e.payload, StepCommitted) for c in e.payload.step.commands if isinstance(c.effect, InvokeTool)}
                executed = [e.payload.outcome.value for e in events if isinstance(e.payload, CommandOutcomeReceived) and calls[e.payload.command_id].name == "execute_command"]
                assert executed and all(value["ok"] for value in executed), executed
                assert all(f.version is not None for f in project_workspace_versions(events))
                first_read = next(e.payload.step.step_id for e in events if isinstance(e.payload, StepCommitted) and any(c.effect.name == "read_file" for c in e.payload.step.commands))
                (root / "user.txt").write_text("human-created")
                started = time.monotonic()
                await host.restart_from_step("test-browser", "coding", first_read, "branch", "travel", restore_files=True)
                timings["travel_seconds"] = time.monotonic() - started
                assert (root / "calc.py").read_text() == original
                assert not (root / "ignored.txt").exists()
                assert (root / "user.txt").read_text() == "human-created"
                branch = await SqliteJournal(host.store.require("branch")).snapshot("branch")
                assert any(isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == WORKSPACE_RESCUE_FACT and e.payload.data["error"] is None for e in branch)
                assert host.is_paused("branch")
                (tmp_path / "result.json").write_text(json.dumps({"model": selected_model, "timings": timings, "tools": progress}, indent=2))
                print("real coding + time travel passed:", timings, flush=True)
            finally:
                failure.cancel(); waiting.cancel()
                await asyncio.gather(failure, waiting, return_exceptions=True)
    asyncio.run(scenario())
