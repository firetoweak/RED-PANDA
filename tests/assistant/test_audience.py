from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from unittest.mock import AsyncMock

from redpanda.assistant.agent_messages import MESSAGE_USER, message_bindings
from redpanda.assistant.agents import AgentCreated, PrivateMessage, Speaker, fact_arguments
from redpanda.assistant.conversations import project_conversation
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.sessions import session_view
from redpanda.automation.once import TIME_REACHED
from redpanda.automation.tool import SCHEDULE_ONCE
from redpanda.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal, ToolBinding, replay
from redpanda.runtime.model import rejected_tool_arguments
from tests.assistant.test_runner import ScriptedDecisionMaker
from tests.session_scheduler import settle_session


async def ok(_context, _arguments):
    return {"ok": True}


class DeliveryAudienceTest(unittest.IsolatedAsyncioTestCase):
    async def test_closing_text_goes_to_the_person_only_for_turns_the_person_started(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions")
            await store.create("ada", workspace_id="project", initial_fact=fact_arguments(AgentCreated(
                name="Ada", workspace_id="project", worktree=directory, branch="ada"), delivery_id="created"))
            journal = SqliteJournal(store.require("ada"))
            runtime = AgentRuntime(journal, ScriptedDecisionMaker((
                lambda _: ModelDecision(content="我来负责测试。"),
                lambda _: ModelDecision(content="回给人"),
                lambda _: ModelDecision(command_requests=(InvokeTool("probe"),)),
                lambda _: ModelDecision(command_requests=(InvokeTool(MESSAGE_USER, (("text", "结论"),)),)),
                lambda _: ModelDecision(command_requests=(InvokeTool(MESSAGE_USER, (("text", "   "),)),)),
                lambda _: ModelDecision(command_requests=(InvokeTool(MESSAGE_USER, rejected_tool_arguments(
                    '{"text": "截断', code="INVALID_JSON", error="截断", hint="重发")),)),
                lambda _: ModelDecision(content="工作记录"),
                lambda _: ModelDecision(command_requests=(InvokeTool(SCHEDULE_ONCE),)),
                lambda _: ModelDecision(content="已安排"),
                lambda _: ModelDecision(content="到点了，告诉人"),
            )), {"probe": ToolBinding(ok), SCHEDULE_ONCE: ToolBinding(ok),
                 MESSAGE_USER: message_bindings(AsyncMock(return_value=None), journal)[MESSAGE_USER]})
            await settle_session(runtime, "ada")
            await runtime.receive_user_message("ada", "你好", delivery_id="hi")
            await settle_session(runtime, "ada")
            await runtime.receive_domain_fact("ada", **fact_arguments(PrivateMessage(
                message_id="m-1", sender=Speaker(kind="agent", name="Lin"), recipient="Ada",
                thread_id="m-1", hop=0, reply_to=None, text="看一下", origin="o-1"), delivery_id="m-1"))
            await settle_session(runtime, "ada")
            await runtime.receive_user_message("ada", "十分钟后提醒我", delivery_id="remind")
            await settle_session(runtime, "ada")
            state = replay("ada", await journal.snapshot("ada")).state
            schedule_id = next(item.command.command_id for item in state.commands
                               if item.command.effect.name == SCHEDULE_ONCE)
            await runtime.receive_domain_fact(
                "ada", TIME_REACHED, {"schedule_id": schedule_id, "purpose": "提醒"},
                source="automation", delivery_id=schedule_id, requests_decision=True)
            await settle_session(runtime, "ada")

            events = await journal.snapshot("ada")
            state = replay("ada", events)
            view = project_conversation("ada", events, state.state.steps, session=session_view(state.state))
            audience = {item.text: item.audience for item in view.items if item.kind == "step" and item.text}
            # Calls that were rejected reached nobody and stay tool records, not messages to the person.
            rejected = [tool for item in view.items if item.kind == "step"
                        for tool in item.tools if tool.name == MESSAGE_USER]
            self.assertEqual(len(rejected), 2)
            self.assertEqual(audience, {
                "我来负责测试。": "user",
                "回给人": "user",
                "结论": "user",
                "工作记录": "log",
                "已安排": "user",
                "到点了，告诉人": "user",
            })
