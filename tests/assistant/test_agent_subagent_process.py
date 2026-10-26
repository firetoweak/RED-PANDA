"""Shared files, child execution, human authorization and resource reclamation across Workers."""
from __future__ import annotations

import asyncio
from functools import partial
import json
import sys

import pytest

from konden.assistant.conversations import AssistantQueries
from konden.assistant.commands import COMMAND_FINISHED
from konden.assistant.host.session_store import SessionStore
from konden.assistant.host.supervisor import HostSupervisor
from konden.assistant.subagent.subagent import project_delegate_intents, project_reclaimed
from konden.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from konden.paths import KondenHome
from konden.runtime import StepCommitted
from tests.assistant.test_agent_worktrees_process import git, tree
from tests.execution.test_host_process_process import shell, until
from tests.fixtures.session_worker import config_for, is_subagent_request

pytestmark = pytest.mark.process


class DelegatingLlm:
    def __init__(self, long):
        self.long = long
        self.directory = None
        self.parent_replies = []

    async def chat(self, messages, model, *, tools=None, **kwargs):
        text = json.dumps(messages, ensure_ascii=False)
        names = [call["function"]["name"] for message in messages for call in message.get("tool_calls", ())]
        call = None
        if is_subagent_request(tools):
            if "execute_command" not in names:
                code = "from pathlib import Path;import time;assert Path('note.txt').read_text()=='uncommitted';Path('child.txt').write_text('shared');print('VERIFIED',flush=True)"
                if self.long:
                    code += ";Path('ready').touch();time.sleep(90);Path('late').touch()"
                call = ToolCall("run", "execute_command", json.dumps({"command": shell(sys.executable, "-u", "-c", code), "wait_seconds": 0}))
            elif COMMAND_FINISHED in text:
                assert "VERIFIED" in text
                return self.reply("CHILD_DONE verified shared files")
            else:
                return self.reply("子任务命令正在执行")
        elif "delegate" not in names:
            call = ToolCall("delegate", "delegate", json.dumps({"task": "验证共享文件并执行命令", "cwd": str(self.directory)}))
        elif "CHILD_DONE" in text and "reclaim" not in names:
            child = next(json.loads(message["content"])["data"]["child_session_id"] for message in messages
                         if message["role"] == "tool" and '"DELEGATED"' in str(message.get("content")))
            call = ToolCall("end", "reclaim", json.dumps({"child_session_id": child, "reason": "验证已验收"}))
        if call is not None:
            return LLMCallResult(LLMResponse(content="", calls=(call,)), LLMUsage(input_tokens=1, output_tokens=1))
        self.parent_replies.append("PARENT_RESPONSIVE")
        return self.reply("PARENT_RESPONSIVE")

    @staticmethod
    def reply(text):
        return LLMCallResult(LLMResponse(content=text), LLMUsage(input_tokens=1, output_tokens=1))


@pytest.mark.parametrize("long", [False, True])
def test_child_executes_in_shared_uncommitted_files_and_reclaim_releases_commands(tmp_path, long):
    async def scenario():
        project = tmp_path / "project"
        project.mkdir()
        git(project, "init")
        (project / "note.txt").write_text("initial")
        git(project, "add", "note.txt")
        git(project, "commit", "-m", "initial")
        home = KondenHome(tmp_path / "home")
        llm = DelegatingLlm(long)
        from konden.filesystem.registry import WorkspaceRegistry
        workspace = WorkspaceRegistry.load(home.workspaces_path).register_path(project)
        host = HostSupervisor(SessionStore(home.runtime_sessions_root), partial(config_for, project), home, None, llm=llm)
        try:
            parent = await host.create_agent(name="parent", workspace_id=workspace.workspace_id)
            root = tree(parent)
            llm.directory = root
            (root / "note.txt").write_text("uncommitted")
            before = git(root, "rev-parse", "HEAD")
            await host.set_auto_authorize(not long)
            await host.accept_input(parent.session_id, "运行子任务", delivery_id="start")
            async def delegated():
                return bool(project_delegate_intents(await host.store.journal(parent.session_id).snapshot(parent.session_id)))
            await until(delegated)
            events = await host.store.journal(parent.session_id).snapshot(parent.session_id)
            [intent] = project_delegate_intents(events)
            child = intent.child_session_id
            if long:
                queries = AssistantQueries(host.store, host)
                async def pending():
                    return bool(await queries.list_inbox())
                await until(pending)
                [item] = await queries.list_inbox()
                assert item.session_id == parent.session_id
                assert item.command_session_id == child
                assert not (root / "child.txt").exists()
                assert (await queries.agent_entry(parent.session_id)).status == "authorization"
                await host.resolve_child_authorization(parent.session_id, child, item.command_id, approved=True)
                async def ready():
                    return (root / "ready").exists()
                await until(ready)
                await host.accept_input(parent.session_id, "先回答这个新问题", delivery_id="question")
                async def answered():
                    return len(llm.parent_replies) >= 2
                await until(answered)
                assert any(command["state"] == "running" for command in await host.list_commands(child))
                listing = await host._list_subagents(parent.session_id)
                assert listing["data"]["subagents"][0]["status"] == "running"
                await host._route("reclaim_child", child, {"parent_session_id": parent.session_id, "reason": "测试回收"})
                assert all(command["state"] != "running" for command in await host.list_commands(child))
                assert not (root / "late").exists()
                from konden.execution.command import CommandStartError
                with pytest.raises(CommandStartError):
                    await host.commands.manager.execute(child, "after-reclaim", "echo should-not-run", root, 0)
            else:
                async def reclaimed():
                    return child in project_reclaimed(await host.store.journal(parent.session_id).snapshot(parent.session_id))
                await until(reclaimed)
            assert (root / "child.txt").read_text() == "shared"
            assert git(root, "rev-parse", "HEAD") == before
            assert not (project / "child.txt").exists()
            child_events = await host.store.journal(child).snapshot(child)
            assert sum(command.effect.name == "execute_command" for event in child_events if isinstance(event.payload, StepCommitted)
                       for command in event.payload.step.commands) == 1
            assert host.failures.empty()
        finally:
            await host.close()
    asyncio.run(scenario())
