"""直接目录装配不依赖 AgentFS；真实 Shell 必须使用给定的 worktree。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory

import pytest

from redpanda.assistant.assembly import build_assistant_assembly
from redpanda.assistant.subagent.subagent import DELEGATE, RECLAIM, REPORT
from redpanda.config import AssistantConfig
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage
from redpanda.paths import RedPandaHome
from redpanda.runtime import MemoryJournal
from redpanda.runtime.dispatcher import AttemptContext
from redpanda.tools.builtin import create_subagent_workspace_specs
from tests.fixtures.workspaces import workspace_record
from tests.session_scheduler import SettlingScheduler


class CapturingLlm:
    async def chat(self, messages, model, *, tools=None):
        self.messages = messages
        self.tools = tools
        return LLMCallResult(LLMResponse(content="done"), LLMUsage(input_tokens=1, output_tokens=1))


class DirectWorkspaceTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_files_and_model_surface_do_not_use_agentfs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            home = RedPandaHome(root / "home")
            llm = CapturingLlm()
            with (
                patch("redpanda.assistant.assembly.workspace_files", side_effect=AssertionError("AgentFS")),
                patch("redpanda.assistant.assembly.ChildFiles", side_effect=AssertionError("AgentFS child")),
                patch("redpanda.assistant.assembly.WorkspaceVersionBoundary", side_effect=AssertionError("AgentFS history")),
            ):
                assembly = await build_assistant_assembly(
                    AssistantConfig(model_name="test", compact_threshold_tokens=200_000, llm=llm),
                    lambda *_: None, MemoryJournal(), session_id="agent",
                    workspace=workspace_record(workspace), home=home,
                    direct_workspace=True, scheduler_factory=SettlingScheduler,
                )
                try:
                    context = AttemptContext("agent", "write", "attempt-write", 1)
                    write = assembly.bindings["write_file"]
                    self.assertIs(write.requires_authorization, True)
                    result = await write.handler(context, {"path": "note.txt", "content": "direct"})
                    self.assertTrue(result["ok"])
                    self.assertEqual((workspace / "note.txt").read_text(), "direct")
                    result = await assembly.bindings["read_file"].handler(context, {"path": "note.txt"})
                    self.assertTrue(result["ok"])
                    result = await write.handler(context, {"path": str(root / "outside.txt"), "content": "outside"})
                    self.assertFalse(result["ok"])
                    self.assertFalse((root / "outside.txt").exists())
                    self.assertIs(assembly.bindings["execute_command"].requires_authorization, True)
                    self.assertIsNone(assembly.scheduler.record_workspace_versions)
                    await assembly.sessions.create("agent")
                    await assembly.sessions.receive_user_message("agent", "hello", delivery_id="input")
                    await assembly.scheduler.join()
                    names = {tool["function"]["name"] for tool in llm.tools}
                    self.assertTrue({"read_file", "write_file", "execute_command"} <= names)
                    unavailable = {
                        "get_changes", "restore_workspace", DELEGATE, RECLAIM, REPORT,
                        *(spec.name for spec in create_subagent_workspace_specs(None)),
                    }
                    self.assertFalse(unavailable & names)
                    self.assertFalse(unavailable & assembly.bindings.keys())
                    command = next(tool["function"] for tool in llm.tools if tool["function"]["name"] == "execute_command")
                    self.assertNotIn("file_access", command["parameters"]["properties"])
                    self.assertNotIn("managed", command["description"])
                    environment = next(message["content"] for message in llm.messages if "<environment>" in str(message["content"]))
                    self.assertIn("当前不提供文件回退", environment)
                    self.assertIn("主动核对、暂存和提交自己的修改", environment)
                    self.assertNotIn("不擅自提交或推送代码", environment)
                    self.assertIn("git show", environment)
                    self.assertNotIn("file_access=managed", environment)
                    self.assertFalse((home.state_root / "workspace_views").exists())
                finally:
                    await assembly.scheduler.close()

    @pytest.mark.process
    async def test_command_runs_in_real_git_worktree_without_native_components(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            (project / "note.txt").write_text("original")

            def git(*arguments):
                return subprocess.run(["git", "-C", str(project), *arguments], check=True, capture_output=True)

            git("init")
            git("add", "note.txt")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
            worktree = root / "agent"
            git("worktree", "add", "-b", "agent", str(worktree))
            with (
                patch("redpanda.assistant.assembly.workspace_files", side_effect=AssertionError("AgentFS")),
                patch("redpanda.filesystem.file_view.client.native_executable", side_effect=AssertionError("native service")),
                patch.dict(os.environ, {"REDPANDA_FILESYSTEM_EXECUTABLE": str(root / "missing-agentfs")}),
            ):
                assembly = await build_assistant_assembly(
                    AssistantConfig(model_name="test", compact_threshold_tokens=200_000, llm=CapturingLlm()),
                    lambda *_: None, MemoryJournal(), session_id="agent",
                    workspace=workspace_record(worktree), home=RedPandaHome(root / "home"),
                    direct_workspace=True,
                )
                try:
                    command = ("Set-Content -LiteralPath note.txt -Value command; (Get-Location).Path"
                               if os.name == "nt" else "printf 'command\\n' > note.txt; pwd")
                    result = await assembly.bindings["execute_command"].handler(
                        AttemptContext("agent", "command", "attempt-command", 1), {"command": command},
                    )
                    self.assertEqual(result["code"], "COMMAND_COMPLETED", result)
                    self.assertEqual((worktree / "note.txt").read_text().strip(), "command")
                    self.assertEqual((project / "note.txt").read_text(), "original")
                    self.assertEqual(git("status", "--porcelain").stdout, b"")
                finally:
                    await assembly.scheduler.close()
