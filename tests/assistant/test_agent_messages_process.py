from __future__ import annotations

import asyncio
from functools import partial
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

import pytest

from redpanda.assistant.agent_messages import DECLARE_DIRECTION, SEND_GROUP_MESSAGE, SEND_MESSAGE
from redpanda.assistant.agents import (
    COLLEAGUE_JOINED, DIRECTION_DECLARED, GROUP_ATTENTION, PRIVATE_MESSAGE, parse_agent_fact,
)
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from redpanda.paths import RedPandaHome
from redpanda.runtime import AgentRuntime, CommandPhase, DomainFactCommitted, InvokeTool, ModelDecision, SqliteJournal, StepCommitted, ToolBinding, replay
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.assistant.test_supervisor import until
from tests.fixtures.agent_messages import interrupted_send_config
from tests.fixtures.session_worker import ProcessLlm, config_for


pytestmark = pytest.mark.process


class MessagingLlm(ProcessLlm):
    def __init__(self, root):
        super().__init__(root)
        self.requests = []

    async def chat(self, messages, model, *, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools})
        text = json.dumps(messages, ensure_ascii=False)
        called = {call["function"]["name"] for message in messages for call in message.get("tool_calls", ())}
        calls = ()
        if "轮到你回应这条群消息" in json.dumps(messages[-1], ensure_ascii=False):
            calls = (ToolCall("group", SEND_GROUP_MESSAGE, json.dumps({"group": "开发组", "text": "收到"})),)
        elif "SEND_TO_LIN" in text:
            if "list_colleagues" not in called:
                calls = (ToolCall("colleagues", "list_colleagues", "{}"),)
            elif SEND_MESSAGE not in called:
                calls = (ToolCall("send", SEND_MESSAGE, json.dumps({"recipient": "Lin", "text": "请核对接口"})),)
        elif "Kai" in text:
            if "list_colleagues" not in called:
                calls = (ToolCall("colleagues", "list_colleagues", "{}"),)
            elif DECLARE_DIRECTION not in called:
                calls = (ToolCall("declare", DECLARE_DIRECTION, json.dumps({"direction": "负责命令执行"})),)
        return LLMCallResult(LLMResponse(content="" if calls else "done", calls=calls),
                             LLMUsage(input_tokens=10, output_tokens=5))


class AgentMessagesProcessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.home = RedPandaHome(self.root / "home")
        self.store = SessionStore(self.home.runtime_sessions_root)
        project = self.root / "project"
        project.mkdir()
        (project / "note.txt").write_text("initial")
        for args in (("init",), ("add", "."), ("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")):
            subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)
        self.workspace = WorkspaceRegistry.load(self.home.workspaces_path).register_path(project)
        self.llm = MessagingLlm(self.root)
        self.host = self.new_host()
        self.ada = await self.host.create_agent(name="Ada", workspace_id=self.workspace.workspace_id)
        self.lin = await self.host.create_agent(name="Lin", workspace_id=self.workspace.workspace_id)
        # Creation wakes each Agent once; tests start from settled, offline Agents.
        for agent in (self.ada, self.lin):
            await self.settled(agent)
            await until(lambda: agent.session_id not in self.host.workers)

    def new_host(self, when=None):
        factory = partial(config_for, self.root) if when is None else partial(interrupted_send_config, self.root, when)
        return HostSupervisor(self.store, factory, self.home, None, llm=self.llm)

    async def asyncTearDown(self):
        await self.host.close()
        self.directory.cleanup()

    async def events(self, agent):
        return await SqliteJournal(self.store.require(agent.session_id)).snapshot(agent.session_id)

    async def settled(self, agent):
        await asyncio.wait_for(self.host.wait_quiescent(agent.session_id), 30)

    async def test_created_agent_wakes_without_human_and_its_direction_reaches_colleagues(self):
        steps = {agent.session_id: sum(isinstance(e.payload, StepCommitted) for e in await self.events(agent))
                 for agent in (self.ada, self.lin)}
        self.assertTrue(all(steps.values()))
        kai = await self.host.create_agent(name="Kai", workspace_id=self.workspace.workspace_id)
        await self.settled(kai)
        for agent in (self.ada, self.lin):
            await self.settled(agent)
        self.assertTrue(any("你刚加入这个项目" in json.dumps(r["messages"], ensure_ascii=False)
                            and "Kai" in json.dumps(r["messages"], ensure_ascii=False) for r in self.llm.requests))
        roster = {entry.agent.name: entry.direction for entry in await self.host.agents.list()}
        self.assertEqual(roster, {"Ada": None, "Kai": "负责命令执行", "Lin": None})
        for agent in (self.ada, self.lin):
            events = await self.events(agent)
            facts = [(e.payload.fact_type, parse_agent_fact(e.payload).name) for e in events
                     if isinstance(e.payload, DomainFactCommitted)
                     and e.payload.fact_type in {COLLEAGUE_JOINED, DIRECTION_DECLARED}]
            self.assertEqual(facts[-2:], [(COLLEAGUE_JOINED, "Kai"), (DIRECTION_DECLARED, "Kai")])
            # Roster changes are visible later but do not make colleagues decide.
            self.assertEqual(sum(isinstance(e.payload, StepCommitted) for e in events), steps[agent.session_id])
        self.assertTrue(self.host.failures.empty())

    async def test_group_members_are_called_in_turn_and_speak_through_their_tool(self):
        group = await self.host.agent_groups.create(workspace_id=self.workspace.workspace_id, name="开发组",
                                                    members=["Ada", "Lin"])
        await self.host.agent_groups.post(group.group_id, "大家同步一下进度", [])
        drive = self.host.agent_groups._drives[group.group_id]
        while drive.task is not None and not drive.task.done():
            await asyncio.wait_for(drive.task, 60)
        for agent in (self.ada, self.lin):
            await self.settled(agent)
        speakers = [message.sender.name for message in await self.host.agent_groups.messages(group.group_id)]
        # Every speech without a mention calls the other member, until the round limit.
        self.assertEqual(speakers[0], None)
        self.assertEqual(sorted(speakers[1:]), ["Ada"] * 3 + ["Lin"] * 3)
        for agent in (self.ada, self.lin):
            attentions = [e for e in await self.events(agent) if isinstance(e.payload, DomainFactCommitted)
                          and e.payload.fact_type == GROUP_ATTENTION]
            self.assertEqual(len(attentions), 3)
        self.assertTrue(self.host.failures.empty())

    async def test_paused_offline_recipient_gets_fact_but_no_step_until_human_continues(self):
        # Persist the human pause without creating a Worker, as after a Host restart.
        self.host._pause.set(self.lin.session_id, True)
        self.assertNotIn(self.lin.session_id, self.host.workers)
        before = await self.events(self.lin)
        await self.host.receive_user_message(self.ada.session_id, "SEND_TO_LIN", delivery_id="send")
        await self.settled(self.ada)
        await self.settled(self.lin)
        after = await self.events(self.lin)
        messages = [event for event in after if isinstance(event.payload, DomainFactCommitted)
                    and event.payload.fact_type == PRIVATE_MESSAGE]
        self.assertEqual(len(messages), 1)
        self.assertEqual(sum(isinstance(e.payload, StepCommitted) for e in before),
                         sum(isinstance(e.payload, StepCommitted) for e in after))
        self.assertTrue(self.host.is_paused(self.lin.session_id))
        command = next(item for item in replay(self.ada.session_id, await self.events(self.ada)).state.commands
                       if item.command.effect.name == SEND_MESSAGE)
        self.assertTrue(command.outcome.value["data"]["paused"])
        self.assertIn("在人让它继续后处理", command.outcome.value["data"]["notice"])
        await self.host.set_paused(self.lin.session_id, False)
        await self.settled(self.lin)
        self.assertTrue(any(isinstance(e.payload, StepCommitted) for e in await self.events(self.lin)))
        for request in self.llm.requests:
            self.assertNotIn(self.ada.session_id, json.dumps(request["messages"]))
            self.assertNotIn(self.workspace.workspace_id, json.dumps(request["messages"]))
        self.assertTrue(self.host.failures.empty())

    async def test_offline_recipient_is_started_and_can_observe_attributed_message(self):
        self.assertNotIn(self.lin.session_id, self.host.workers)
        await self.host.receive_user_message(self.ada.session_id, "SEND_TO_LIN", delivery_id="send")
        await self.settled(self.ada)
        await self.settled(self.lin)
        messages = [parse_agent_fact(e.payload) for e in await self.events(self.lin)
                    if isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == PRIVATE_MESSAGE]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender.name, "Ada")
        self.assertTrue(any(isinstance(e.payload, StepCommitted) for e in await self.events(self.lin)))
        self.assertTrue(any("请核对接口" in json.dumps(r["messages"], ensure_ascii=False) for r in self.llm.requests))
        self.assertTrue(self.host.failures.empty())

    async def test_real_sender_crash_recovers_one_delivery_and_a_known_original_outcome(self):
        for when in ("before", "after"):
            with self.subTest(when=when):
                await self.host.close()
                # Each crash exercises a fresh sender with a fresh Command.
                self.host = self.new_host(when)
                sender = await self.host.create_agent(name=f"Sender-{when}", workspace_id=self.workspace.workspace_id)
                await self.host.receive_user_message(sender.session_id, "SEND_TO_LIN", delivery_id="send")
                failure = await asyncio.wait_for(self.host.wait_failure(), 30)
                self.assertEqual(failure.session_id, sender.session_id)
                await self.host.close()
                self.host = self.new_host()
                await self.host.resume(sender.session_id)
                await self.settled(sender)
                await self.settled(self.lin)
                command = next(item for item in replay(sender.session_id, await self.events(sender)).state.commands
                               if item.command.effect.name == SEND_MESSAGE)
                self.assertIsNot(command.phase, CommandPhase.UNKNOWN)
                self.assertEqual(command.outcome.value["code"], "DELIVERED")
                received = [parse_agent_fact(e.payload) for e in await self.events(self.lin)
                            if isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == PRIVATE_MESSAGE]
                self.assertEqual(sum(m.sender.name == sender.name for m in received), 1)
                self.assertTrue(self.host.failures.empty())

    async def test_two_workers_recovering_mutual_sends_can_accept_facts_before_deciding(self):
        for sender, recipient in ((self.ada, self.lin), (self.lin, self.ada)):
            entered = asyncio.Event()

            async def interrupted(context, arguments):
                entered.set()
                await asyncio.Event().wait()

            journal = SqliteJournal(self.store.require(sender.session_id))
            runtime = AgentRuntime(journal, ScriptedDecisionMaker((lambda _, name=recipient.name: ModelDecision(
                command_requests=(InvokeTool(SEND_MESSAGE, (("recipient", name), ("text", "恢复中的私信"))),)),)),
                {SEND_MESSAGE: ToolBinding(interrupted)})
            await runtime.receive_user_message(sender.session_id, "互发私信", delivery_id="mutual")
            await runtime.advance(sender.session_id)
            await runtime.dispatcher.start_pending(sender.session_id)
            await asyncio.wait_for(entered.wait(), 3)
            await runtime.dispatcher.close()
        await asyncio.wait_for(asyncio.gather(
            self.host.resume(self.ada.session_id), self.host.resume(self.lin.session_id)), 30)
        await self.settled(self.ada)
        await self.settled(self.lin)
        for agent in (self.ada, self.lin):
            events = await self.events(agent)
            messages = [e for e in events if isinstance(e.payload, DomainFactCommitted)
                        and e.payload.fact_type == PRIVATE_MESSAGE]
            self.assertEqual(len(messages), 1)
            command, = [item for item in replay(agent.session_id, events).state.commands
                        if item.command.effect.name == SEND_MESSAGE]
            self.assertEqual(command.outcome.value["code"], "DELIVERED")
        self.assertTrue(self.host.failures.empty())
