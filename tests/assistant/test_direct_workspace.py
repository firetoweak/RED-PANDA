"""Agent 在自己的 worktree 里直接工作；真实 Shell 必须使用给定的 worktree。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest
from tempfile import TemporaryDirectory
from functools import partial

from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from tests.fixtures.session_worker import config_for

import pytest

from redpanda.assistant.assembly import build_assistant_assembly
from redpanda.assistant.subagent.subagent import DELEGATE
from redpanda.config import AssistantConfig
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage
from redpanda.paths import RedPandaHome
from tests.fixtures.memory_journal import MemoryJournal
from redpanda.runtime.dispatcher import AttemptContext
from tests.fixtures.workspaces import workspace_record
from tests.session_scheduler import SettlingScheduler


async def no_host_settings(operation, *args):
    return "continue" if operation == "compact_boundary" else None


class CapturingLlm:
    async def chat(self, messages, model, *, tools=None):
        self.messages = messages
        self.tools = tools
        return LLMCallResult(LLMResponse(content="done"), LLMUsage(input_tokens=1, output_tokens=1))


class DirectWorkspaceTest(unittest.IsolatedAsyncioTestCase):
    async def test_agent_writes_its_worktree_and_sees_branch_tools(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            home = RedPandaHome(root / "home")
            llm = CapturingLlm()
            assembly = await build_assistant_assembly(
                AssistantConfig(model_name="test", compact_threshold_tokens=200_000, llm=llm),
                lambda *_: None, MemoryJournal(), session_id="agent",
                workspace=workspace_record(workspace), home=home,
                agent_branch=True, scheduler_factory=SettlingScheduler, session_transport=no_host_settings,
            )
            try:
                context = AttemptContext("agent", "write", "attempt-write", 1)
                write = assembly.bindings["write_file"]
                self.assertIs(write.requires_authorization({"path": "note.txt"}), True)
                result = await write.handler(context, {"path": "note.txt", "content": "direct"})
                self.assertTrue(result["ok"])
                self.assertEqual((workspace / "note.txt").read_text(), "direct")
                result = await assembly.bindings["read_file"].handler(context, {"path": "note.txt"})
                self.assertTrue(result["ok"])
                result = await write.handler(context, {"path": str(root / "outside.txt"), "content": "outside"})
                self.assertFalse(result["ok"])
                self.assertFalse((root / "outside.txt").exists())
                self.assertIs(assembly.bindings["execute_command"].requires_authorization, True)
                await assembly.sessions.create("agent")
                await assembly.sessions.receive_user_message("agent", "hello", delivery_id="input")
                await assembly.scheduler.join()
                names = {tool["function"]["name"] for tool in llm.tools}
                self.assertTrue({"read_file", "write_file", "execute_command"} <= names)
                delegate = next(tool["function"] for tool in llm.tools if tool["function"]["name"] == DELEGATE)
                self.assertIn("merge_subagent 合入你的分支", delegate["description"])
                command = next(tool["function"] for tool in llm.tools if tool["function"]["name"] == "execute_command")
                self.assertNotIn("file_access", command["parameters"]["properties"])
                environment = next(message["content"] for message in llm.messages if "<environment>" in str(message["content"]))
                self.assertIn("当前不提供文件回退", environment)
                self.assertIn("主动核对、暂存和提交自己的修改", environment)
                self.assertNotIn("不擅自提交或推送代码", environment)
                self.assertIn("git show", environment)
            finally:
                await assembly.scheduler.close()

    @pytest.mark.process
    async def test_command_runs_in_real_git_worktree(self):
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
            home = RedPandaHome(root / "home")
            store = SessionStore(home.runtime_sessions_root)
            registered = WorkspaceRegistry.load(home.workspaces_path).register_path(worktree)
            host = HostSupervisor(store, partial(config_for, worktree), home, None, llm=CapturingLlm())
            await host.create("agent", registered.workspace_id)
            assembly = await build_assistant_assembly(
                AssistantConfig(model_name="test", compact_threshold_tokens=200_000, llm=CapturingLlm()),
                lambda *_: None, MemoryJournal(), session_id="agent",
                workspace=workspace_record(worktree), home=home,
                agent_branch=True, session_transport=host._route,
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
                await host.close()
