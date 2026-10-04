from __future__ import annotations

import asyncio
import unittest

from redpanda.runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    CommandPhase,
    DomainFactCommitted,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    ToolBinding,
    UserMessageReceived,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.runtime.test_boundary_slice import RecordingTool, runtime_for
from tests.session_scheduler import SettlingScheduler


class RuntimeSemanticSliceTest(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_command_group_waits_for_all_outcomes(self):
        left = asyncio.Event()
        right = asyncio.Event()

        async def wait_left(_context, _arguments):
            await left.wait()
            return "left"

        async def wait_right(_context, _arguments):
            await right.wait()
            return "right"

        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool("left"),
                        InvokeTool("right"),
                    )
                ),
                lambda _frame: ModelDecision(content="both done"),
            )
        )
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {
                "left": ToolBinding(wait_left),
                "right": ToolBinding(wait_right),
            },
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await scheduler.wake("session")
        await asyncio.sleep(0)

        left.set()
        await asyncio.sleep(0)
        self.assertEqual(len(model.frames), 1)
        right.set()
        await scheduler.join()

        state = await runtime.state("session")
        self.assertEqual(len(model.frames), 2)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        await scheduler.close()

    async def test_domain_fact_requesting_decision_becomes_next_trigger(self):
        model = ScriptedDecisionMaker(
            (lambda _frame: ModelDecision(content="noted"),)
        )
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        await runtime.create_session("session")
        await runtime.receive_domain_fact(
            "session",
            "subagent.report",
            {"child_session_id": "child-1"},
            delivery_id="report-1",
            source="subagent",
            requests_decision=True,
        )

        self.assertEqual(
            (await runtime.state("session")).status,
            RuntimeStatus.RUNNABLE,
        )
        await runtime.advance("session")

        self.assertEqual(len(model.frames), 1)
        self.assertEqual(
            model.frames[0].trigger_event.payload.fact_type,
            "subagent.report",
        )

    async def test_domain_fact_without_decision_request_stays_quiet(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(()),
            {},
            SequentialIds(),
        )
        await runtime.create_session("session")
        await runtime.receive_domain_fact(
            "session",
            "subagent.progress",
            {"note": "still running"},
            delivery_id="progress-1",
            source="subagent",
        )

        state = await runtime.state("session")
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertIsNone(state.next_trigger_event_id)

    async def test_later_domain_fact_waits_for_in_flight_attempt(self):
        tool = RecordingTool("work")
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(content="stopped"),
            )
        )
        runtime = runtime_for(tool, model)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await runtime.advance("session")
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_domain_fact(
            "session",
            "subagent.report",
            {"summary": "done"},
            delivery_id="report-1",
            source="subagent",
            requests_decision=True,
        )

        state = await runtime.state("session")
        self.assertEqual(len(model.frames), 1)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertIsNone(state.next_trigger_event_id)
        self.assertTrue(any(item.startswith("command:") for item in state.waiting_for))

        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        await runtime.advance("session")

        self.assertEqual(len(model.frames), 2)
        self.assertEqual(
            model.frames[1].trigger_event.payload.fact_type,
            "subagent.report",
        )

    async def test_later_domain_fact_suppresses_outcome_follow_up_step(self):
        tool = RecordingTool("work")
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(content="ack"),
            )
        )
        runtime = runtime_for(tool, model)
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await scheduler.wake("session")
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_domain_fact(
            "session",
            "subagent.report",
            {"summary": "done"},
            delivery_id="report-1",
            source="subagent",
            requests_decision=True,
        )
        await scheduler.wake("session")
        tool.release.set()
        await scheduler.join()

        self.assertEqual(len(model.frames), 2)
        self.assertEqual(
            model.frames[1].trigger_event.payload.fact_type,
            "subagent.report",
        )
        kinds = [
            type(event.payload).__name__
            for event in await runtime.snapshot("session")
        ]
        self.assertEqual(kinds.count("StepCommitted"), 2)
        self.assertEqual(kinds.count("CommandOutcomeReceived"), 1)
        self.assertEqual(kinds.count(DomainFactCommitted.__name__), 1)
        await scheduler.close()

    async def test_later_user_message_waits_for_in_flight_attempt(self):
        tool = RecordingTool("work")
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(content="stopped"),
            )
        )
        runtime = runtime_for(tool, model)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await runtime.advance("session")
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_user_message(
            "session",
            "stop",
            delivery_id="user-2",
        )

        state = await runtime.state("session")
        self.assertEqual(len(model.frames), 1)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertIsNone(state.next_trigger_event_id)
        self.assertTrue(any(item.startswith("command:") for item in state.waiting_for))

        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        await runtime.advance("session")

        self.assertEqual(len(model.frames), 2)
        self.assertEqual(model.frames[1].trigger_event.payload.content, "stop")
        self.assertEqual(
            (await runtime.state("session")).status,
            RuntimeStatus.WAITING,
        )

    async def test_later_user_message_suppresses_outcome_follow_up_step(self):
        tool = RecordingTool("work")
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(content="ack"),
            )
        )
        runtime = runtime_for(tool, model)
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await scheduler.wake("session")
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_user_message(
            "session",
            "never mind",
            delivery_id="user-2",
        )
        await scheduler.wake("session")
        tool.release.set()
        await scheduler.join()

        self.assertEqual(len(model.frames), 2)
        self.assertEqual(model.frames[1].trigger_event.payload.content, "never mind")
        kinds = [
            type(event.payload).__name__
            for event in await runtime.snapshot("session")
        ]
        self.assertEqual(kinds.count("StepCommitted"), 2)
        self.assertEqual(kinds.count("CommandOutcomeReceived"), 1)
        await scheduler.close()

    async def test_later_user_message_step_includes_waited_outcomes(self):
        tool = RecordingTool("work")
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(content="saw result"),
            )
        )
        runtime = runtime_for(tool, model)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        first = await runtime.advance("session")
        command_id = first.step.commands[0].command_id
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_user_message(
            "session",
            "look",
            delivery_id="user-2",
        )
        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        await runtime.advance("session")

        visible = model.frames[1].state.visible_event_ids
        events = await runtime.snapshot("session")
        outcome_ids = [
            event.event_id
            for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
            and event.payload.command_id == command_id
        ]
        self.assertEqual(len(outcome_ids), 1)
        self.assertTrue(set(outcome_ids) <= set(visible))
        self.assertIsNotNone(model.frames[1].state.command(command_id).outcome)

    async def test_two_later_user_messages_first_step_still_sees_waited_outcomes(
        self,
    ):
        tool = RecordingTool("work")
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(content="first later"),
                lambda _frame: ModelDecision(content="second later"),
            )
        )
        runtime = runtime_for(tool, model)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        first = await runtime.advance("session")
        command_id = first.step.commands[0].command_id
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_user_message(
            "session",
            "stop",
            delivery_id="user-2",
        )
        later = await runtime.receive_user_message(
            "session",
            "do this instead",
            delivery_id="user-3",
        )
        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        await runtime.advance("session")

        self.assertEqual(model.frames[1].trigger_event.payload.content, "stop")
        visible = model.frames[1].state.visible_event_ids
        events = await runtime.snapshot("session")
        outcome_ids = [
            event.event_id
            for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
            and event.payload.command_id == command_id
        ]
        self.assertEqual(len(outcome_ids), 1)
        self.assertTrue(set(outcome_ids) <= set(visible))
        self.assertNotIn(later.event_id, visible)
        self.assertIsNotNone(model.frames[1].state.command(command_id).outcome)

        await runtime.advance("session")
        self.assertEqual(
            model.frames[2].trigger_event.payload.content,
            "do this instead",
        )
        self.assertEqual(len(model.frames), 3)

    async def test_second_burst_fact_sees_first_fact_step_outcomes(self):
        """一批外部事实里，为前一条提交的 Step 也要带着自己的命令终局出现。

        该 Step 因果上早于后一条事实，却是在它到达之后才提交的，
        观测位置因此越过了它。两者必须一起可见，否则冻结出的这一帧
        会带着一个没有结果的工具调用。
        """

        tool = RecordingTool("work")
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(content="done"),
            )
        )
        runtime = runtime_for(tool, model)
        await runtime.create_session("session")
        await runtime.receive_user_message("session", "go", delivery_id="user-1")
        await runtime.advance("session")
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        for index in (1, 2):
            await runtime.receive_domain_fact(
                "session",
                "subagent.report",
                {"child_session_id": f"child-{index}"},
                delivery_id=f"report-{index}",
                source="subagent",
                requests_decision=True,
            )
        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)

        second = await runtime.advance("session")
        self.assertEqual(
            model.frames[1].trigger_event.payload.data["child_session_id"],
            "child-1",
        )
        command_id = second.step.commands[0].command_id
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)

        await runtime.advance("session")

        frame = model.frames[2]
        self.assertEqual(
            frame.trigger_event.payload.data["child_session_id"],
            "child-2",
        )
        events = await runtime.snapshot("session")
        outcome_ids = {
            event.event_id
            for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
            and event.payload.command_id == command_id
        }
        self.assertEqual(len(outcome_ids), 1)
        self.assertTrue(outcome_ids <= set(frame.state.visible_event_ids))
        self.assertIsNotNone(frame.state.command(command_id).outcome)

    async def test_later_user_message_after_group_closes_still_owns_next_step(self):
        tool = RecordingTool("work")
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(content="after close"),
            )
        )
        runtime = runtime_for(tool, model)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await runtime.advance("session")
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        later = await runtime.receive_user_message(
            "session",
            "wait",
            delivery_id="user-2",
        )
        state = await runtime.state("session")
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)
        self.assertEqual(state.next_trigger_event_id, later.event_id)
        await runtime.advance("session")
        self.assertEqual(model.frames[1].trigger_event.payload.content, "wait")
        self.assertEqual(len(model.frames), 2)

    async def test_queued_user_message_before_step_does_not_suppress_outcome_group(
        self,
    ):
        tool = RecordingTool("work")
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                lambda _frame: ModelDecision(content="second"),
                lambda _frame: ModelDecision(content="after tools"),
            )
        )
        runtime = runtime_for(tool, model)
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "one",
            delivery_id="user-1",
        )
        await runtime.receive_user_message(
            "session",
            "two",
            delivery_id="user-2",
        )
        await scheduler.wake("session")
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        tool.release.set()
        await scheduler.join()

        messages = [
            frame.trigger_event.payload.content
            for frame in model.frames
            if isinstance(frame.trigger_event.payload, UserMessageReceived)
        ]
        self.assertEqual(messages, ["one", "two"])
        self.assertEqual(len(model.frames), 3)
        await scheduler.close()

    async def test_user_message_during_thinking_runs_same_step_commands_only(
        self,
    ):
        tool = RecordingTool("work")
        thinking = asyncio.Event()
        release_think = asyncio.Event()

        async def continue_old(_frame):
            thinking.set()
            await release_think.wait()
            return ModelDecision(command_requests=(InvokeTool("work"),))

        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("work"),),
                ),
                continue_old,
                lambda _frame: ModelDecision(content="stopped"),
            )
        )
        runtime = runtime_for(tool, model)
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await scheduler.wake("session")
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        tool.release.set()
        await asyncio.wait_for(thinking.wait(), timeout=1)

        await runtime.receive_user_message(
            "session",
            "stop",
            delivery_id="user-2",
        )
        tool.started = asyncio.Event()
        tool.release = asyncio.Event()
        release_think.set()
        await asyncio.wait_for(tool.started.wait(), timeout=1)

        state = await runtime.state("session")
        self.assertEqual(len(model.frames), 2)
        self.assertFalse(
            isinstance(model.frames[1].trigger_event.payload, UserMessageReceived)
        )
        self.assertIsNone(state.next_trigger_event_id)
        self.assertTrue(any(item.startswith("command:") for item in state.waiting_for))

        tool.release.set()
        await scheduler.join()

        self.assertEqual(len(model.frames), 3)
        self.assertEqual(model.frames[2].trigger_event.payload.content, "stop")
        events = await runtime.snapshot("session")
        kinds = [type(event.payload).__name__ for event in events]
        self.assertEqual(kinds.count("StepCommitted"), 3)
        outcome_events = [
            event
            for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
        ]
        self.assertEqual(len(outcome_events), 2)
        second_outcome = outcome_events[1]
        visible = model.frames[2].state.visible_event_ids
        self.assertIn(second_outcome.event_id, visible)
        self.assertIsNotNone(
            model.frames[2].state.command(second_outcome.payload.command_id).outcome
        )
        await scheduler.close()

    async def test_later_user_message_does_not_wait_for_unauthorized_command(self):
        tool = RecordingTool("secret", requires_authorization=True)
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool("secret"),),
                ),
                lambda _frame: ModelDecision(content="new direction"),
            )
        )
        runtime = runtime_for(tool, model)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await runtime.advance("session")
        later = await runtime.receive_user_message(
            "session",
            "wait don't",
            delivery_id="user-2",
        )
        state = await runtime.state("session")
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)
        self.assertEqual(state.next_trigger_event_id, later.event_id)
        self.assertFalse(tool.started.is_set())

        await runtime.advance("session")
        self.assertEqual(model.frames[1].trigger_event.payload.content, "wait don't")
        self.assertFalse(tool.started.is_set())

    async def test_later_user_message_preserves_event_order(self):
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(content="first"),
                lambda _frame: ModelDecision(content="second"),
            )
        )
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "one",
            delivery_id="user-1",
        )
        await runtime.receive_user_message(
            "session",
            "two",
            delivery_id="user-2",
        )
        await scheduler.wake("session")
        await scheduler.join()

        messages = [
            frame.trigger_event.payload.content
            for frame in model.frames
            if isinstance(frame.trigger_event.payload, UserMessageReceived)
        ]
        self.assertEqual(messages, ["one", "two"])
        await scheduler.close()

    async def test_tool_error_is_not_wrapped_and_attempt_stays_unknown(self):
        async def explode(_context, _arguments):
            raise LookupError("raw failure")

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(InvokeTool("explode"),),
                    ),
                )
            ),
            {"explode": ToolBinding(explode)},
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await scheduler.wake("session")

        with self.assertRaisesRegex(LookupError, "raw failure"):
            await scheduler.join()
        state = await runtime.state("session")
        self.assertEqual(
            state.commands[0].phase,
            CommandPhase.UNKNOWN,
        )
        await scheduler.close()
