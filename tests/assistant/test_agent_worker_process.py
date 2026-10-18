"""Host-selected Agent worktrees survive real Worker and Host restarts."""
from __future__ import annotations

import asyncio
from functools import partial
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pytest

from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from redpanda.paths import RedPandaHome
from tests.fixtures.session_worker import ProcessLlm, direct_workspace_config
from tests.fixtures.compact_worker import HANDOFF


pytestmark = pytest.mark.process


class WorktreeLlm(ProcessLlm):
    def __init__(self, root):
        super().__init__(root)
        self.requests = []

    async def chat(self, messages, model, *, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools})
        if any("<self_handoff>" in str(message["content"]) for message in messages):
            return LLMCallResult(LLMResponse(content=HANDOFF), LLMUsage(input_tokens=10, output_tokens=5))
        last_input = max((i for i, message in enumerate(messages) if message["role"] == "user"), default=-1)
        text = messages[last_input]["content"] if last_input >= 0 else ""
        results = {call["function"]["name"] for message in messages[last_input + 1:]
                   for call in message.get("tool_calls", ())}
        calls = ()
        if isinstance(text, str) and text.startswith("WRITE_AGENT_"):
            if "write_file" not in results:
                calls = (ToolCall("agent-write", "write_file", json.dumps({
                    "path": "note.txt", "content": text, "overwrite": True,
                })),)
            elif "execute_command" not in results:
                command = ("Set-Content -LiteralPath command.txt -Value command-ok"
                           if os.name == "nt" else "printf 'command-ok\\n' > command.txt")
                calls = (ToolCall("agent-command", "execute_command", json.dumps({"command": command})),)
        elif text == "READ_AGENT" and "read_file" not in results:
            calls = (ToolCall("agent-read", "read_file", '{"path":"note.txt"}'),)
        return LLMCallResult(LLMResponse(content="" if calls else "done", calls=calls),
                             LLMUsage(input_tokens=10, output_tokens=5))


class AgentWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_host_restart_keeps_project_identity_and_each_agents_real_execution_root(self):
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
            (project / "note.txt").write_text("human-uncommitted")
            original_status = git("status", "--porcelain").stdout
            home = RedPandaHome(root / "home")
            store = SessionStore(home.runtime_sessions_root)
            workspace = WorkspaceRegistry.load(home.workspaces_path).register_path(project)
            llm = WorktreeLlm(root)

            def host():
                return HostSupervisor(store, partial(direct_workspace_config, root), home, None, llm=llm)

            current = host()
            with patch.dict(os.environ, {"REDPANDA_FILESYSTEM_EXECUTABLE": str(root / "missing-agentfs")}):
                try:
                    agents = []
                    for index in range(2):
                        agent = await current.create_agent(
                            name=f"同事{index}", workspace_id=workspace.workspace_id,
                        )
                        worktree = agent.worktree
                        agents.append(agent)
                        await current.select(f"page-{index}", agent.session_id)
                        await current.set_auto_authorize(agent.session_id, True)
                        await current.receive_user_message(agent.session_id, f"WRITE_AGENT_{index}", delivery_id="write")
                        await asyncio.wait_for(current.wait_quiescent(agent.session_id), 30)
                        self.assertEqual((worktree / "note.txt").read_text(), f"WRITE_AGENT_{index}")
                        self.assertEqual((worktree / "command.txt").read_text().strip(), "command-ok")
                        self.assertEqual(await current.bound_workspace_id(agent.session_id), workspace.workspace_id)
                    self.assertFalse((home.state_root / "workspace_views").exists())
                    self.assertTrue(current.failures.empty())
                    await current.close()
                    current = host()
                    recovered = await current.agents.resolve(workspace.workspace_id, "同事0")
                    self.assertEqual(recovered, agents[0])
                    await current.select("new-page", recovered.session_id)
                    await current.receive_user_message(recovered.session_id, "READ_AGENT", delivery_id="read")
                    await asyncio.wait_for(current.wait_quiescent(recovered.session_id), 30)
                    read_results = [message["content"] for request in llm.requests for message in request["messages"]
                                    if message["role"] == "tool" and json.loads(message["content"])["code"] == "FILE_READ"]
                    self.assertTrue(read_results)
                    self.assertIn("WRITE_AGENT_0", read_results[-1])
                    snapshot = await current.request("compact_snapshot", recovered.session_id, {})
                    job = current.compact.store.start(recovered.session_id, snapshot)
                    await current.compact.ensure_reader(job)
                    async with asyncio.timeout(30):
                        while current.conversation_status(recovered.session_id).compact_count != 1:
                            await asyncio.sleep(0.02)
                    self.assertEqual(len(await current.agents.list()), 2)
                    self.assertTrue(any("<self_handoff>" in str(message["content"])
                                        for request in llm.requests for message in request["messages"]))
                    for request in llm.requests:
                        names = {tool["function"]["name"] for tool in request["tools"]}
                        self.assertFalse({"get_changes", "restore_workspace", "delegate"} & names)
                        self.assertNotIn(workspace.workspace_id, json.dumps(request["messages"]))
                        for agent in agents:
                            self.assertNotIn(agent.session_id, json.dumps(request["messages"]))
                    self.assertFalse((home.state_root / "workspace_views").exists())
                    self.assertTrue(current.failures.empty())
                    self.assertEqual((project / "note.txt").read_text(), "human-uncommitted")
                    self.assertEqual(git("status", "--porcelain").stdout, original_status)
                finally:
                    await current.close()
