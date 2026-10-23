from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pytest

from redpanda.assistant.agent_messages import DECLARE_DIRECTION, SEND_GROUP_MESSAGE, SEND_MESSAGE
from redpanda.assistant.agents import (
    AGENT_CREATED, COLLEAGUE_JOINED, DIRECTION_DECLARED, GROUP_ATTENTION, PRIVATE_MESSAGE, parse_agent_fact,
)
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.shared_memory import READ_SHARED_MEMORY, UPDATE_SHARED_MEMORY, project_shared_memory
from redpanda.automation.routines import RoutineFire
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
        attention = None
        # A passive group speech can follow the call before the decision freezes.
        # Answer calls since our last response, even when they are not the last message.
        for message in reversed(messages):
            if message["role"] == "assistant":
                break
            if f'"fact": "{GROUP_ATTENTION}"' in str(message.get("content")):
                attention = json.loads(message["content"])["data"]
                break
        if attention is not None:
            calls = (ToolCall("group", SEND_GROUP_MESSAGE, json.dumps(
                {"group": attention["group"], "text": "收到", "reply_to": attention["message_id"]})),)
        elif "MEMORY_WRITE:" in text:
            if READ_SHARED_MEMORY not in called:
                calls = (ToolCall("read-memory", READ_SHARED_MEMORY, "{}"),)
            elif UPDATE_SHARED_MEMORY not in called:
                current = next(json.loads(message["content"])["data"] for message in reversed(messages)
                               if message["role"] == "tool" and "SHARED_MEMORY_READ" in str(message["content"]))
                instruction = next(message["content"] for message in reversed(messages)
                                   if message["role"] == "user" and str(message["content"]).startswith("MEMORY_WRITE:"))
                calls = (ToolCall("write-memory", UPDATE_SHARED_MEMORY, json.dumps({
                    "version": current["version"], "content": instruction.removeprefix("MEMORY_WRITE:"),
                })),)
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
        self.llm = MessagingLlm(self.root)
        self.host = self.new_host()
        self.ada = await self.host.create_agent(name="Ada")
        self.lin = await self.host.create_agent(name="Lin")
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
        kai = await self.host.create_agent(name="Kai")
        await self.settled(kai)
        for agent in (self.ada, self.lin):
            await self.settled(agent)
        self.assertTrue(any(f'"fact": "{AGENT_CREATED}"' in str(message["content"])
                            and '"name": "Kai"' in str(message["content"])
                            for r in self.llm.requests for message in r["messages"]))
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

    async def test_plain_user_and_routine_replies_reach_person_without_waking_colleague(self):
        outputs = []
        self.host.sink = lambda *args: outputs.append(args)
        before = await self.events(self.lin)
        await self.host.receive_user_message(self.ada.session_id, "只回复 done", delivery_id="plain")
        await self.settled(self.ada)
        now = datetime.now(timezone.utc)
        await self.host._deliver_routine(RoutineFire(
            fire_id="routine-fire", routine_id="routine", session_id=self.ada.session_id,
            name="检查", intent="报告状态", planned_at=now, fired_at=now))
        await self.settled(self.ada)
        self.assertEqual([args[2] for args in outputs], ["done", "done"])
        self.assertTrue(all(args[0] == self.ada.session_id for args in outputs))
        self.assertEqual(await self.events(self.lin), before)
        self.assertNotIn(self.lin.session_id, self.host.workers)
        self.assertTrue(self.host.failures.empty())

    async def test_both_bots_edit_one_memory_and_updates_do_not_wake_idle_colleagues(self):
        idle = await self.events(self.lin)
        await self.host.receive_user_message(self.ada.session_id, "MEMORY_WRITE:共享资源先协调。", delivery_id="ada-memory")
        await self.settled(self.ada)
        self.assertEqual(await self.events(self.lin), idle)
        self.assertNotIn(self.lin.session_id, self.host.workers)
        self.assertEqual(project_shared_memory(await self.events(self.ada))["content"], "共享资源先协调。")
        await self.host.receive_user_message(self.lin.session_id, "MEMORY_WRITE:共享资源先协调，交接附验证依据。", delivery_id="lin-memory")
        await self.settled(self.lin)
        note = project_shared_memory(await self.events(self.lin))
        self.assertEqual(note, {"version": 2, "content": "共享资源先协调，交接附验证依据。", "updated_by": "Lin"})
        await self.host.receive_user_message(self.ada.session_id, "查看同事的新约定。", delivery_id="observe-memory")
        await self.settled(self.ada)
        self.assertEqual(project_shared_memory(await self.events(self.ada)), note)
        self.assertIn("共享资源先协调，交接附验证依据。", json.dumps(self.llm.requests[-1]["messages"], ensure_ascii=False))
        self.assertTrue(self.host.failures.empty())

    async def test_group_members_receive_calls_even_after_current_turn_interruption(self):
        await self.host.cancel_turn(self.lin.session_id)
        outputs = []
        self.host.sink = lambda *args: outputs.append(args)
        group = await self.host.agent_groups.create(name="开发组",
                                                    members=["Ada", "Lin"])
        await self.host.agent_groups.post(group.group_id, "大家同步一下进度", [])
        # Busy members can speak after the drive temporarily runs out of queued calls.
        # Wait for the expected public replies, not that intermediate scheduler state.
        async with asyncio.timeout(60):
            while len(await self.host.agent_groups.messages(group.group_id)) < 7:
                await asyncio.sleep(0.02)
            await until(lambda: not self.host.agent_groups._drives[group.group_id].running)
        for agent in (self.ada, self.lin):
            await self.settled(agent)
        speakers = [message.sender.name for message in await self.host.agent_groups.messages(group.group_id)]
        # Every speech without a mention calls the other member, until the round limit.
        self.assertEqual(speakers[0], None)
        self.assertEqual(sorted(speakers[1:]), ["Ada"] * 3 + ["Lin"] * 3)
        self.assertTrue(any(args[2] == "done" for args in outputs))
        for agent in (self.ada, self.lin):
            attentions = [e for e in await self.events(agent) if isinstance(e.payload, DomainFactCommitted)
                          and e.payload.fact_type == GROUP_ATTENTION]
            self.assertEqual(len(attentions), 3)
        self.assertTrue(self.host.failures.empty())

    async def test_group_delivery_continues_while_a_called_member_is_still_deciding(self):
        group = await self.host.agent_groups.create(name="开发组", members=["Ada", "Lin"])
        entered, release = asyncio.Event(), asyncio.Event()
        route = self.host._route

        async def held(operation, session_id, arguments, *, worker=None):
            if operation == "llm_chat" and session_id == self.ada.session_id:
                entered.set()
                await release.wait()
            return await route(operation, session_id, arguments, worker=worker)

        async def lin_has_replied_to(message_id):
            return any(message.sender.name == "Lin" and message.reply_to == message_id
                       for message in await self.host.agent_groups.messages(group.group_id))

        with patch.object(self.host, "_route", side_effect=held):
            first = await self.host.agent_groups.post(group.group_id, "两位验收第一版", [])
            try:
                await asyncio.wait_for(entered.wait(), 30)
                async with asyncio.timeout(30):
                    while not await lin_has_replied_to(first.message_id):
                        await asyncio.sleep(.02)
                second = await self.host.agent_groups.post(group.group_id, "Lin 再看下一版", ["Lin"])
                async with asyncio.timeout(30):
                    while not await lin_has_replied_to(second.message_id):
                        await asyncio.sleep(.02)
                self.assertFalse(release.is_set())
            finally:
                release.set()
            for agent in (self.ada, self.lin):
                await self.settled(agent)
        self.assertTrue(self.host.failures.empty())

    async def test_interrupted_recipient_processes_new_peer_message_automatically(self):
        await self.host.cancel_turn(self.lin.session_id)
        before = await self.events(self.lin)
        await self.host.receive_user_message(self.ada.session_id, "SEND_TO_LIN", delivery_id="send")
        await self.settled(self.ada)
        await self.settled(self.lin)
        after = await self.events(self.lin)
        messages = [event for event in after if isinstance(event.payload, DomainFactCommitted)
                    and event.payload.fact_type == PRIVATE_MESSAGE]
        self.assertEqual(len(messages), 1)
        self.assertGreater(sum(isinstance(e.payload, StepCommitted) for e in after),
                           sum(isinstance(e.payload, StepCommitted) for e in before))
        command = next(item for item in replay(self.ada.session_id, await self.events(self.ada)).state.commands
                       if item.command.effect.name == SEND_MESSAGE)
        self.assertEqual(command.outcome.value["data"]["notice"], "已送达；不等待对方回复。")
        for request in self.llm.requests:
            self.assertNotIn(self.ada.session_id, json.dumps(request["messages"]))
            self.assertIn("link_project", {tool["function"]["name"] for tool in request["tools"]})
        self.assertTrue(self.host.failures.empty())

    async def test_offline_recipient_is_started_and_can_observe_attributed_message(self):
        outputs = []
        self.host.sink = lambda *args: outputs.append(args)
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
        self.assertTrue(any(args[0] == self.lin.session_id and args[2] == "done" for args in outputs))
        self.assertFalse(any(isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == PRIVATE_MESSAGE
                             for e in await self.events(self.ada)))
        self.assertTrue(self.host.failures.empty())

    async def test_real_sender_crash_recovers_one_delivery_and_a_known_original_outcome(self):
        for when in ("before", "after"):
            with self.subTest(when=when):
                await self.host.close()
                # Each crash exercises a fresh sender with a fresh Command.
                self.host = self.new_host(when)
                sender = await self.host.create_agent(name=f"Sender-{when}")
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
