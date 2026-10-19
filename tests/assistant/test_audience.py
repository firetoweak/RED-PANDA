from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from unittest.mock import AsyncMock

from redpanda.assistant.agent_messages import MESSAGE_USER, message_bindings
from redpanda.assistant.agents import AgentCreated, fact_arguments
from redpanda.assistant.conversations import project_conversation
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.sessions import session_view
from redpanda.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal, replay
from redpanda.runtime.model import rejected_tool_arguments
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.session_scheduler import settle_session


class DeliveryAudienceTest(unittest.IsolatedAsyncioTestCase):
    async def view(self, store, session_id, decisions, *, initial_fact=None, bindings=lambda journal: {}):
        await store.create(session_id, workspace_id="project", initial_fact=initial_fact)
        journal = SqliteJournal(store.require(session_id))
        runtime = AgentRuntime(journal, ScriptedDecisionMaker(decisions), bindings(journal))
        await settle_session(runtime, session_id)
        await runtime.receive_user_message(session_id, "你好", delivery_id="hi")
        await settle_session(runtime, session_id)
        events = await journal.snapshot(session_id)
        state = replay(session_id, events)
        return project_conversation(session_id, events, state.state.steps, session=session_view(state.state))

    async def test_an_agent_reaches_the_person_only_through_message_user(self):
        with TemporaryDirectory() as directory:
            view = await self.view(SessionStore(Path(directory) / "sessions"), "ada", (
                lambda _: ModelDecision(content="我来负责测试。"),
                lambda _: ModelDecision(command_requests=(InvokeTool(MESSAGE_USER, (("text", "结论"),)),)),
                lambda _: ModelDecision(command_requests=(InvokeTool(MESSAGE_USER, (("text", "   "),)),)),
                lambda _: ModelDecision(command_requests=(InvokeTool(MESSAGE_USER, rejected_tool_arguments(
                    '{"text": "截断', code="INVALID_JSON", error="截断", hint="重发")),)),
                lambda _: ModelDecision(content="收尾"),
            ), initial_fact=fact_arguments(AgentCreated(
                name="Ada", workspace_id="project", worktree=directory, branch="ada"), delivery_id="created"),
                bindings=lambda journal: {
                    MESSAGE_USER: message_bindings(AsyncMock(return_value=None), journal)[MESSAGE_USER]})
        audience = {item.text: item.audience for item in view.items if item.kind == "step" and item.text}
        # Closing text stays the work log even in the turn the person started.
        self.assertEqual(audience, {"我来负责测试。": "log", "结论": "user", "收尾": "log"})
        # Calls that were rejected reached nobody and stay tool records, not messages to the person.
        rejected = [tool for item in view.items if item.kind == "step"
                    for tool in item.tools if tool.name == MESSAGE_USER]
        self.assertEqual(len(rejected), 2)

    async def test_an_ordinary_session_answers_the_person_directly(self):
        with TemporaryDirectory() as directory:
            view = await self.view(SessionStore(Path(directory) / "sessions"), "plain",
                                   (lambda _: ModelDecision(content="你好呀"),))
        self.assertEqual([item.audience for item in view.items if item.kind == "step"], ["user"])
