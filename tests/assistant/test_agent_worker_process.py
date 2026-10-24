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

from konden.assistant.host.session_store import SessionStore
from konden.assistant.host.supervisor import HostSupervisor
from konden.assistant.host.agent_roster import AgentDeletedError
from konden.filesystem.registry import WorkspaceRegistry
from konden.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from konden.paths import KondenHome
from konden.runtime import SqliteJournal, replay
from konden.runtime.json_values import thaw_value
from tests.fixtures.session_worker import ProcessLlm, config_for, is_subagent_request
from tests.fixtures.compact_worker import HANDOFF
from tests.assistant.test_agent_worktrees_process import tree


pytestmark = pytest.mark.process


def linked_project(messages):
    """Where the model was told its project worktree is, as a model reading the fact would."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and '"bot.project_linked"' in content:
            data = json.loads(content)["data"]
            return data["project"], data["directory"]
    return None, None


def delayed_memory_sync_config(root):
    from konden.assistant import assembly

    sync = assembly.sync_shared_memory

    async def delayed(*args):
        (root / "memory-sync-started").touch()
        while not (root / "release-memory-sync").exists():
            await asyncio.sleep(0.01)
        return await sync(*args)

    assembly.sync_shared_memory = delayed
    return config_for(root)


class WorktreeLlm(ProcessLlm):
    def __init__(self, root):
        super().__init__(root)
        self.requests = []
        self.read_started = asyncio.Event()
        self.child_started = asyncio.Event()
        self.read_release = asyncio.Event()

    async def chat(self, messages, model, *, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools})
        if is_subagent_request(tools) and "DELETE_CHILD" in str(messages):
            self.child_started.set()
            await self.read_release.wait()
        if any("<self_handoff>" in str(message["content"]) for message in messages):
            return LLMCallResult(LLMResponse(content=HANDOFF), LLMUsage(input_tokens=10, output_tokens=5))
        # Context snapshots also use user messages; only scenario instructions drive this stub.
        last_input = max((i for i, message in enumerate(messages)
                          if message["role"] == "user" and isinstance(message["content"], str)
                          and (message["content"].startswith("WRITE_AGENT_")
                               or message["content"] in {
                                   "FINISH_AGENT", "LAND_AGENT", "DELEGATE_FOR_DELETE", "READ_AGENT",
                               })), default=-1)
        text = messages[last_input]["content"] if last_input >= 0 else ""
        results = {call["function"]["name"] for message in messages[last_input + 1:]
                   for call in message.get("tool_calls", ())}
        commands = {json.loads(call["function"]["arguments"])["command"]
                    for message in messages[last_input + 1:]
                    for call in message.get("tool_calls", ())
                    if call["function"]["name"] == "execute_command"}
        calls = ()
        project, directory = linked_project(messages)
        here = {} if directory is None else {"cwd": directory}
        note = "note.txt" if directory is None else str(Path(directory) / "note.txt")
        if isinstance(text, str) and text.startswith("WRITE_AGENT_"):
            if "write_file" not in results:
                calls = (ToolCall("agent-write", "write_file", json.dumps({
                    "path": note, "content": text, "overwrite": True,
                })),)
            elif "execute_command" not in results:
                command = ("Set-Content -LiteralPath command.txt -Value command-ok"
                           if os.name == "nt" else "printf 'command-ok\\n' > command.txt")
                calls = (ToolCall("agent-command", "execute_command", json.dumps({"command": command, **here})),)
        elif text == "FINISH_AGENT":
            stage = "git add -- note.txt command.txt"
            commit = 'git commit -m "record agent changes"'
            if stage not in commands:
                calls = (ToolCall("agent-stage", "execute_command", json.dumps({
                    "command": stage, **here,
                })),)
            elif commit not in commands:
                calls = (ToolCall("agent-commit", "execute_command", json.dumps({
                    "command": commit, **here,
                })),)
        elif text == "LAND_AGENT" and "land_branch" not in results:
            calls = (ToolCall("agent-land", "land_branch", json.dumps({"project": project})),)
        elif text == "DELEGATE_FOR_DELETE" and "delegate" not in results:
            calls = (ToolCall("child-delete", "delegate", json.dumps({"task": "DELETE_CHILD"})),)
        elif text == "READ_AGENT" and "read_file" not in results:
            self.read_started.set()
            await self.read_release.wait()
            calls = (ToolCall("agent-read", "read_file", json.dumps({"path": note})),)
        return LLMCallResult(LLMResponse(content="" if calls else "done", calls=calls),
                             LLMUsage(input_tokens=10, output_tokens=5))


class AgentWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_delete_stops_the_worker_and_command_and_restart_keeps_it_read_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = KondenHome(root / "home")
            home.initialize()
            store = SessionStore(home.runtime_sessions_root)
            llm = WorktreeLlm(root)
            host = HostSupervisor(store, partial(config_for, root), home, None, llm=llm)
            try:
                agent = await host.create_agent(name="Ada")
                await asyncio.wait_for(host.wait_quiescent(agent.session_id), 30)
                (agent.directory / "成果.txt").write_text("保留", encoding="utf-8")
                await host.receive_user_message(agent.session_id, "DELEGATE_FOR_DELETE", delivery_id="child")
                await asyncio.wait_for(llm.child_started.wait(), 30)
                command = "Start-Sleep -Seconds 60" if os.name == "nt" else "sleep 60"
                result = await host.commands.manager.execute(agent.session_id, "owned-command", command, agent.directory, 0)
                await host.receive_user_message(agent.session_id, "READ_AGENT", delivery_id="read")
                await asyncio.wait_for(llm.read_started.wait(), 30)
                await asyncio.wait_for(host.delete_agent(agent.session_id), 30)
                self.assertEqual(host.workers, {})
                stopped = await host.commands.manager.lookup(agent.session_id, result["command_id"])
                self.assertEqual(stopped["state"], "stopped")
                with self.assertRaises(AgentDeletedError):
                    await host.receive_user_message(agent.session_id, "不能再执行", delivery_id="after-delete")
                self.assertTrue(host.failures.empty())
            finally:
                await host.close()
            restored = HostSupervisor(store, partial(config_for, root), home, None, llm=llm)
            try:
                before = len(llm.requests)
                await restored.resume_unfinished_sessions()
                await restored.select("history", agent.session_id)
                self.assertEqual(await restored.agents.list(), ())
                self.assertEqual(restored.workers, {})
                self.assertEqual(len(llm.requests), before)
                self.assertEqual((agent.directory / "成果.txt").read_text(encoding="utf-8"), "保留")
            finally:
                await restored.close()

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
            base = git("rev-parse", "HEAD").stdout
            git("config", "user.name", "Human")
            git("config", "user.email", "human@example.com")
            repository_config = (project / ".git" / "config").read_bytes()
            global_config = root / "gitconfig"
            global_config.write_text('[user]\n\tname = Global Human\n\temail = global@example.com\n')
            original_global_config = global_config.read_bytes()
            (project / "note.txt").write_text("human-uncommitted")
            original_status = git("status", "--porcelain").stdout
            home = KondenHome(root / "home")
            store = SessionStore(home.runtime_sessions_root)
            workspace = WorkspaceRegistry.load(home.workspaces_path).register_path(project)
            llm = WorktreeLlm(root)

            def host():
                return HostSupervisor(store, partial(config_for, root), home, None, llm=llm)

            with patch.dict(os.environ, {
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_AUTHOR_NAME": "Inherited Human",
                "GIT_AUTHOR_EMAIL": "inherited@example.com",
                "GIT_COMMITTER_NAME": "Inherited Human",
                "GIT_COMMITTER_EMAIL": "inherited@example.com",
            }):
                current = host()
                try:
                    agents = []
                    for index in range(2):
                        agent = await current.create_agent(
                            name=f"同事{index}", workspace_id=workspace.workspace_id,
                        )
                        worktree = tree(agent)
                        agents.append(agent)
                        await current.select(f"page-{index}", agent.session_id)
                        await current.set_auto_authorize(True)
                        await current.receive_user_message(agent.session_id, f"WRITE_AGENT_{index}", delivery_id="write")
                        await asyncio.wait_for(current.wait_quiescent(agent.session_id), 30)
                        self.assertEqual((worktree / "note.txt").read_text(), f"WRITE_AGENT_{index}")
                        self.assertEqual((worktree / "command.txt").read_text().strip(), "command-ok")
                        # Completing write and reply Steps must not stage or commit.
                        self.assertEqual(git("rev-parse", agent.projects[0].branch).stdout, base)
                        self.assertEqual([item.workspace_id for item in agent.projects], [workspace.workspace_id])
                    self.assertFalse((home.state_root / "workspace_views").exists())
                    self.assertTrue(current.failures.empty())
                    await current.receive_user_message(agents[0].session_id, "FINISH_AGENT", delivery_id="finish")
                    await asyncio.wait_for(current.wait_quiescent(agents[0].session_id), 30)
                    committed = git("rev-parse", agents[0].projects[0].branch).stdout
                    self.assertNotEqual(committed, base)
                    self.assertEqual(git("show", "-s", "--format=%an%n%ae%n%cn%n%ce",
                                         agents[0].projects[0].branch).stdout.decode().splitlines(),
                                     ["同事0", "bot@konden.local", "同事0", "bot@konden.local"])
                    self.assertEqual(git("show", f"{agents[0].projects[0].branch}:note.txt").stdout.decode(), "WRITE_AGENT_0")
                    self.assertEqual(git("diff", "--name-only", base.decode().strip(),
                                         agents[0].projects[0].branch).stdout.decode().splitlines(), ["command.txt", "note.txt"])
                    self.assertEqual(git("rev-parse", agents[1].projects[0].branch).stdout, base)
                    await current.receive_user_message(agents[0].session_id, "LAND_AGENT", delivery_id="land")
                    await asyncio.wait_for(current.wait_quiescent(agents[0].session_id), 30)
                    integration = current.agent_worktrees.integration(workspace.workspace_id)
                    self.assertEqual(git("rev-parse", integration.branch).stdout, committed)
                    self.assertEqual((integration.path / "note.txt").read_text(), "WRITE_AGENT_0")
                    await current.close()
                    current = host()
                    recovered = await current.agents.resolve("同事0")
                    self.assertEqual(recovered, agents[0])
                    await current.select("new-page", recovered.session_id)
                    await current.receive_user_message(recovered.session_id, "READ_AGENT", delivery_id="read")
                    waiting = asyncio.create_task(current.wait_quiescent(recovered.session_id))
                    try:
                        await asyncio.wait_for(llm.read_started.wait(), 30)
                        await asyncio.sleep(0)
                        self.assertFalse(waiting.done(), "Host reported idle while the model was still deciding")
                    finally:
                        llm.read_release.set()
                    await asyncio.wait_for(waiting, 30)
                    events = await SqliteJournal(store.require(recovered.session_id)).snapshot(recovered.session_id)
                    state = replay(recovered.session_id, events).state
                    reads = [command for command in state.commands if command.command.effect.name == "read_file"]
                    self.assertEqual(len(reads), 1, {
                        "status": state.status, "waiting": state.waiting_for,
                        "commands": [(command.command.effect.name, command.phase) for command in state.commands],
                        "inputs": [message["content"] for request in llm.requests[-2:]
                                   for message in request["messages"] if message["role"] == "user"],
                    })
                    self.assertIsNotNone(reads[0].outcome)
                    result = thaw_value(reads[0].outcome.value)
                    self.assertEqual(result["code"], "FILE_READ", result)
                    self.assertIn("WRITE_AGENT_0", json.dumps(result))
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
                        self.assertTrue({"land_branch", "sync_branch", "delegate"} <= names)
                        self.assertNotIn(workspace.workspace_id, json.dumps(request["messages"]))
                        for agent in agents:
                            self.assertNotIn(agent.session_id, json.dumps(request["messages"]))
                    self.assertFalse((home.state_root / "workspace_views").exists())
                    self.assertTrue(current.failures.empty())
                    self.assertEqual(git("rev-parse", agents[0].projects[0].branch).stdout, committed)
                    self.assertEqual(git("rev-parse", agents[1].projects[0].branch).stdout, base)
                    self.assertEqual((project / "note.txt").read_text(), "human-uncommitted")
                    self.assertEqual(git("status", "--porcelain").stdout, original_status)
                    self.assertEqual((project / ".git" / "config").read_bytes(), repository_config)
                    self.assertEqual(global_config.read_bytes(), original_global_config)
                finally:
                    await current.close()


    async def test_bot_directory_survives_host_restart_and_compaction_without_project(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = KondenHome(root / "home")
            store = SessionStore(home.runtime_sessions_root)
            llm = WorktreeLlm(root)

            def host():
                return HostSupervisor(store, partial(delayed_memory_sync_config, root), home, None, llm=llm)

            current = host()
            try:
                agent = await current.create_agent(name="Notes")
                async with asyncio.timeout(30):
                    while not (root / "memory-sync-started").exists():
                        await asyncio.sleep(0.01)
                self.assertEqual(agent.projects, ())
                self.assertEqual(WorkspaceRegistry.load(home.workspaces_path).workspaces, ())
                await current.select("page", agent.session_id)
                await current.set_auto_authorize(True)
                await current.receive_user_message(agent.session_id, "WRITE_AGENT_0", delivery_id="write")
                # Reproduce a context snapshot arriving after the user's first instruction.
                (root / "release-memory-sync").touch()
                await asyncio.wait_for(current.wait_quiescent(agent.session_id), 30)
                self.assertEqual((agent.directory / "note.txt").read_text(), "WRITE_AGENT_0")
                self.assertEqual((agent.directory / "command.txt").read_text().strip(), "command-ok")
                await current.close()
                current = host()
                self.assertEqual(await current.agents.resolve("Notes"), agent)
                await current.select("page", agent.session_id)
                snapshot = await current.request("compact_snapshot", agent.session_id, {})
                job = current.compact.store.start(agent.session_id, snapshot)
                await current.compact.ensure_reader(job)
                async with asyncio.timeout(30):
                    while current.conversation_status(agent.session_id).compact_count != 1:
                        await asyncio.sleep(0.02)
                llm.read_release.set()
                await current.receive_user_message(agent.session_id, "READ_AGENT", delivery_id="read")
                await asyncio.wait_for(current.wait_quiescent(agent.session_id), 30)
                events = await SqliteJournal(store.require(agent.session_id)).snapshot(agent.session_id)
                commands = replay(agent.session_id, events).state.commands
                result = next(c.outcome for c in commands if c.command.effect.name == "read_file")
                self.assertEqual(thaw_value(result.value)["code"], "FILE_READ")
                self.assertEqual(list(home.agent_worktrees_root.iterdir()), [])
                for request in llm.requests:
                    names = {tool["function"]["name"] for tool in request["tools"]}
                    # Without a project the Bot can still link one mid-conversation.
                    self.assertTrue({"list_colleagues", "link_project"} <= names)
                self.assertTrue(current.failures.empty())
            finally:
                await current.close()
