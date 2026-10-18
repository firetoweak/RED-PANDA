from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from redpanda.assistant.agents import (
    AGENT_CREATED, AgentCreated, GroupAttention, GroupMessage, PrivateMessage,
    Speaker, agent_fact_for_model, fact_arguments, parse_agent_fact,
)
from redpanda.assistant.context.projection import project_chat_messages
from redpanda.runtime import AgentRuntime, DomainFactCommitted, MemoryJournal, ModelDecision, StateProjector
from tests.assistant.test_runner import ScriptedDecisionMaker


class AgentFactTest(unittest.IsolatedAsyncioTestCase):
    async def test_group_history_is_visible_when_attention_requests_a_decision(self):
        model = ScriptedDecisionMaker((lambda _: ModelDecision(content="seen"),))
        runtime = AgentRuntime(MemoryJournal(), model, {})
        await runtime.create_session("recipient")
        message = GroupMessage(
            message_id="message-1", sender=Speaker(kind="agent", name="Ada"),
            thread_id="thread-1", hop=1, reply_to=None, text="接口已经修改，请核对。",
            group="开发组", mentioned_agents=["Lin"], mention_all=False,
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
        self.assertNotIn("thread-1", content)
        self.assertNotIn('"hop"', content)

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
