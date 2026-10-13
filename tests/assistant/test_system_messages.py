from dataclasses import replace
import unittest

from redpanda.assistant.context.projection import ModelContextProjector
from redpanda.assistant.conversations import project_conversation
from redpanda.assistant.sessions import SessionView
from redpanda.assistant.system_messages import SYSTEM_MESSAGE, SystemMessage
from redpanda.runtime import UserMessageReceived, DomainFactCommitted, StateProjector
from tests.assistant.test_loop_guard import event


class SystemMessageTest(unittest.TestCase):
    def test_system_origin_does_not_become_human_input_or_move_dehydration_boundary(self):
        notice = SystemMessage(text="任务可能出现环路", severity="warning", related_session_id=None)
        events = (
            replace(event(1), payload=UserMessageReceived("人的要求")), event(2),
            replace(event(3), payload=DomainFactCommitted(SYSTEM_MESSAGE, notice.model_dump())),
        )
        state = StateProjector().project_visible("s", events)
        prepared = ModelContextProjector().prepare(events, state, "s")
        self.assertEqual(prepared.messages[prepared.protection_start_index]["content"], "人的要求")
        self.assertIn("不是用户新指令", prepared.messages[-1]["content"])
        self.assertEqual(prepared.messages[-2]["role"], "tool")
        self.assertEqual(prepared.messages[-2]["tool_call_id"], prepared.messages[-3]["tool_calls"][0]["id"])
        view = project_conversation("s", events, state.steps,
                                    session=SessionView("waiting", ("external_fact",), (), False))
        self.assertEqual([item.kind for item in view.items], ["user", "step"])
        self.assertEqual(view.system_notice, notice.model_dump())
