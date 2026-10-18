from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from redpanda.automation.routines import ROUTINE_DUE
from redpanda.assistant.agents import (
    AGENT_CREATED, AgentCreated, GroupAttention, GroupMessage, PrivateMessage,
    Speaker, agent_fact_for_model, fact_arguments, parse_agent_fact,
)
from tests.fixtures.projection import project_chat_messages
from redpanda.runtime import AgentRuntime, DomainFactCommitted, ModelDecision, StateProjector
from tests.fixtures.memory_journal import MemoryJournal
from tests.assistant.test_runner import ScriptedDecisionMaker


class AgentFactTest(unittest.IsolatedAsyncioTestCase):
    async def test_group_history_is_visible_when_attention_requests_a_decision(self):
        model = ScriptedDecisionMaker((lambda _: ModelDecision(content="seen"),))
        runtime = AgentRuntime(MemoryJournal(), model, {})
        await runtime.create_session("recipient")
        message = GroupMessage(
            message_id="message-1", sender=Speaker(kind="agent", name="Ada"),
            reply_to=None, text="接口已经修改，请核对。",
            group="开发组", mentioned_agents=["Lin"], origin="origin-internal",
        )
        await runtime.receive_domain_fact("recipient", **fact_arguments(message, delivery_id="message-1"))
        self.assertIsNone((await runtime.advance("recipient")).step)
        self.assertEqual(model.frames, [])
        await runtime.receive_domain_fact("recipient", **fact_arguments(
            GroupAttention(group="开发组", message_id="message-1"), delivery_id="attention-1",
        ))
        self.assertIsNotNone((await runtime.advance("recipient")).step)
        self.assertEqual(len(model.frames), 1)
        events = await runtime.snapshot("recipient")
        shown = project_chat_messages(events, StateProjector().project_visible("recipient", events), "sys")
        content = json.dumps(shown, ensure_ascii=False)
        self.assertIn(message.text, content)
        self.assertIn("Ada", content)
        self.assertIn("开发组", content)
        self.assertIn("不构成用户授权", content)
        self.assertNotIn("origin-internal", content)

    async def test_colleague_and_group_messages_carry_their_arrival_time(self):
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("recipient")
        for message in (
            PrivateMessage(message_id="private-1", sender=Speaker(kind="agent", name="Ada"),
                           recipient="Lin", thread_id="thread-1", hop=0, reply_to=None,
                           text="私信正文", origin="origin-1"),
            GroupMessage(message_id="group-1", sender=Speaker(kind="agent", name="Ada"),
                         reply_to=None, text="群聊正文", group="开发组", mentioned_agents=[],
                         origin="origin-1"),
        ):
            await runtime.receive_domain_fact("recipient", **fact_arguments(message, delivery_id=message.message_id))
        events = await runtime.snapshot("recipient")
        shown = project_chat_messages(events, StateProjector().project_visible("recipient", events), "sys")
        for text in ("私信正文", "群聊正文"):
            index = next(i for i, item in enumerate(shown) if text in str(item["content"]))
            self.assertIn("<message_received_at>", shown[index - 1]["content"])

    async def test_a_routine_reminder_shows_its_intent_times_and_that_it_is_not_a_person(self):
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("owner")
        await runtime.receive_domain_fact(
            "owner", ROUTINE_DUE, {"routine_id": "r-1", "name": "检查 PR", "intent": "按 Skill 检查。",
                                   "planned_at": "2026-10-19T01:00:00+00:00",
                                   "fired_at": "2026-10-19T01:00:02+00:00"},
            source="automation", delivery_id="r-1:1:2026-10-19T01:00:00+00:00", requests_decision=True)
        events = await runtime.snapshot("owner")
        shown = json.dumps(project_chat_messages(
            events, StateProjector().project_visible("owner", events), "sys"), ensure_ascii=False)
        from datetime import datetime
        local_time = datetime.fromisoformat("2026-10-19T01:00:00+00:00").astimezone().isoformat()
        for expected in ("按 Skill 检查。", local_time, "不是人的新消息"):
            self.assertIn(expected, shown)

    def test_model_projection_hides_creation_coordinates_and_keeps_sender_identity(self):
        with TemporaryDirectory() as directory:
            creation = AgentCreated(name="Ada", workspace_id="workspace-internal",
                                    worktree=str(Path(directory).resolve()), branch="internal-branch")
            arguments = fact_arguments(creation, delivery_id="created")
            self.assertTrue(arguments["requests_decision"])
            payload = DomainFactCommitted(arguments["fact_type"], arguments["data"], True)
            shown = agent_fact_for_model(payload)
            self.assertEqual(set(shown), {"name", "notice"})
            self.assertEqual(shown["name"], "Ada")
            for internal in ("workspace-internal", "internal-branch", directory):
                self.assertNotIn(internal, str(shown))
        private = PrivateMessage(
            message_id="message-2", sender=Speaker(kind="agent", name="Ada"),
            recipient="Lin", thread_id="thread-1", hop=2, reply_to="message-1", text="请检查测试。",
            origin="origin-1",
        )
        arguments = fact_arguments(private, delivery_id="command-2")
        payload = DomainFactCommitted(arguments["fact_type"], arguments["data"], arguments["requests_decision"])
        self.assertEqual(parse_agent_fact(payload), private)
        shown = agent_fact_for_model(payload)
        self.assertEqual(shown["sender"], {"kind": "agent", "name": "Ada"})
        self.assertEqual(shown["reply_to"], "message-1")

    def test_persisted_fact_schema_rejects_missing_fields_coercion_and_extra_fields(self):
        with TemporaryDirectory() as directory:
            valid = AgentCreated(name="Ada", workspace_id="workspace-1",
                                 worktree=str(Path(directory).resolve()), branch="agent").model_dump()
            for data in ({key: value for key, value in valid.items() if key != "branch"},
                         {**valid, "name": 42}, {**valid, "mode": "managed"}):
                with self.subTest(data=data), self.assertRaises(ValidationError):
                    parse_agent_fact(DomainFactCommitted(AGENT_CREATED, data, True))
            with self.assertRaises(ValueError):
                parse_agent_fact(DomainFactCommitted(AGENT_CREATED, valid, False))
