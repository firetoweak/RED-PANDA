from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable

from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.decision import decision_from_llm
from helperme.assistant.runner import SessionScheduler
from helperme.llm.api import InvalidLLMResponse, LLMProviderError
from helperme.llm.api import LLMResponse, ToolCall
from helperme.runtime import (
    AgentRuntime,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    UserMessageReceived,
)
from helperme.runtime.state import DecisionFrame
from tests.session_scheduler import SettlingScheduler


DecisionScript = Callable[
    [DecisionFrame],
    ModelDecision | Awaitable[ModelDecision],
]


class SequentialIds:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, prefix: str) -> str:
        self._value += 1
        return f"{prefix}-{self._value}"


class ScriptedDecisionMaker:
    def __init__(self, scripts: tuple[DecisionScript, ...]) -> None:
        self.scripts = scripts
        self.frames: list[DecisionFrame] = []

    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        script = self.scripts[len(self.frames)]
        self.frames.append(frame)
        decision = script(frame)
        if isinstance(decision, Awaitable):
            return await decision
        return decision


class SessionSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_fact_arriving_after_boundary_check_requires_another_check(self):
        model = ScriptedDecisionMaker(())
        runtime = AgentRuntime(MemoryJournal(), model, {})
        scheduler = SessionScheduler(runtime, "session", control=AssistantControlPlane(()))
        await runtime.receive_user_message("session", "small input", delivery_id="first")
        checks = 0

        async def boundary():
            nonlocal checks
            checks += 1
            if checks == 1:
                # A child report arrives while the Host is answering the budget check.
                await runtime.receive_domain_fact(
                    "session", "report", {"text": "large report"},
                    source="child", delivery_id="report", requests_decision=True,
                )
                return True
            return False  # The newly checked input must wait for compaction.

        scheduler.before_advance = boundary
        try:
            self.assertTrue(await scheduler._advance_once())
            self.assertFalse(await scheduler._advance_once())
            self.assertEqual(checks, 2)
            self.assertEqual(model.frames, [])
            self.assertEqual((await runtime.state("session")).decision_cursor, 0)
        finally:
            await scheduler.close()

    async def test_unexpected_failure_is_observable_without_another_wake(self):
        failure = ValueError("host bug")

        async def fail(_frame):
            raise failure

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((fail,)),
            {},
            SequentialIds(),
        )
        scheduler = SessionScheduler(
            runtime,
            "session",
            control=AssistantControlPlane(()),
        )
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "hello",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake("session")
            observed = await asyncio.wait_for(
                scheduler.wait_failure(),
                timeout=1,
            )

            self.assertIs(observed, failure)
            self.assertEqual(
                (await runtime.state("session")).status,
                RuntimeStatus.RUNNABLE,
            )
        finally:
            await scheduler.close()

    async def test_wake_before_done_callback_preserves_original_failure(self):
        failure = ValueError("host bug")
        runtime = AgentRuntime(MemoryJournal(), ScriptedDecisionMaker(()), {})
        scheduler = SessionScheduler(
            runtime, "session", control=AssistantControlPlane(())
        )

        async def fail_then_wake(session_id, **kwargs):
            # Queue wake before asyncio schedules the task's completion callback.
            asyncio.create_task(scheduler.wake(session_id))
            raise failure

        runtime.advance = fail_then_wake
        try:
            await scheduler.wake("session")
            observed = await asyncio.wait_for(scheduler.wait_failure(), timeout=1)
            self.assertIs(observed, failure)
            self.assertTrue(scheduler.idle)
        finally:
            await scheduler.close()

    async def test_recognised_model_failure_can_retry_on_next_wake(self):
        failure = LLMProviderError("provider rejected request")

        async def fail(_frame):
            raise failure

        model = ScriptedDecisionMaker((fail, lambda _frame: ModelDecision(content="ok")))
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        failed = []
        scheduler = SettlingScheduler(
            runtime,
            "session",
            session_failed=lambda session_id, message: failed.append(
                (session_id, message)
            ),
        )
        await runtime.create_session("session")
        await runtime.receive_user_message("session", "hello", delivery_id="user-1")
        try:
            await scheduler.wake("session")
            await scheduler.join()
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0][0], "session")
            self.assertIn("provider rejected request", failed[0][1])
            self.assertTrue(failed[0][1].startswith("运行失败："))
            self.assertIsNone(scheduler._failure)
            self.assertEqual((await runtime.state("session")).status, RuntimeStatus.RUNNABLE)
            await scheduler.wake("session")
            await scheduler.join()
            self.assertEqual((await runtime.state("session")).status, RuntimeStatus.WAITING)
        finally:
            await scheduler.close()

    async def test_empty_model_response_can_retry_on_next_wake(self):
        failure = InvalidLLMResponse(
            "empty_model_response",
            "model response contains neither tool calls nor non-empty text",
        )

        async def fail(_frame):
            raise failure

        model = ScriptedDecisionMaker((fail, lambda _frame: ModelDecision(content="ok")))
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        failed = []
        scheduler = SettlingScheduler(
            runtime,
            "session",
            session_failed=lambda session_id, message: failed.append(
                (session_id, message)
            ),
        )
        await runtime.create_session("session")
        await runtime.receive_user_message("session", "hello", delivery_id="user-1")
        try:
            await scheduler.wake("session")
            await scheduler.join()
            self.assertEqual(len(failed), 1)
            self.assertIn("没有给出可用回复或工具调用", failed[0][1])
            self.assertTrue(failed[0][1].startswith("运行失败："))
            self.assertIsNone(scheduler._failure)
            self.assertEqual((await runtime.state("session")).status, RuntimeStatus.RUNNABLE)
            await scheduler.wake("session")
            await scheduler.join()
            self.assertEqual((await runtime.state("session")).status, RuntimeStatus.WAITING)
        finally:
            await scheduler.close()

    def test_decision_from_llm_maps_text_and_tool_calls(self):
        decision = decision_from_llm(
            LLMResponse(
                content="looking",
                calls=(ToolCall("call-1", "read_file", '{"path":"a.py"}'),),
            ),
            frozenset({"read_file"}),
        )

        self.assertEqual(decision.content, "looking")
        self.assertEqual(decision.command_requests[0].name, "read_file")
        self.assertEqual(
            decision.command_requests[0].argument_dict(),
            {"path": "a.py"},
        )

    async def test_user_event_wakes_one_step_and_session_remains_open(self):
        model = ScriptedDecisionMaker((lambda _frame: ModelDecision(content="done"),))
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")

        try:
            await runtime.receive_user_message(
                "session",
                "hello",
                delivery_id="user-1",
            )
            await scheduler.wake("session")
            await scheduler.join()

            state = await runtime.state("session")
            self.assertEqual(len(state.steps), 1)
            self.assertEqual(state.status, RuntimeStatus.WAITING)
        finally:
            await scheduler.close()

    async def test_later_event_is_the_next_step_trigger(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def first(_frame):
            first_started.set()
            await release_first.wait()
            return ModelDecision(content="first")

        model = ScriptedDecisionMaker(
            (
                first,
                lambda _frame: ModelDecision(content="second"),
            )
        )
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        scheduler = SettlingScheduler(runtime, "session")
        await runtime.create_session("session")

        try:
            await runtime.receive_user_message(
                "session",
                "one",
                delivery_id="user-1",
            )
            await scheduler.wake("session")
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await runtime.receive_user_message(
                "session",
                "two",
                delivery_id="user-2",
            )
            await scheduler.wake("session")
            release_first.set()
            await asyncio.wait_for(scheduler.join(), timeout=1)

            self.assertEqual(len((await runtime.state("session")).steps), 2)
            messages = [
                frame.trigger_event.payload.content
                for frame in model.frames
                if isinstance(frame.trigger_event.payload, UserMessageReceived)
            ]
            self.assertEqual(messages, ["one", "two"])
        finally:
            await scheduler.close()

    async def test_wake_rejects_another_session(self):
        model = ScriptedDecisionMaker(())
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        scheduler = SettlingScheduler(runtime, "session")
        try:
            with self.assertRaises(AssertionError):
                await scheduler.wake("another-session")
            self.assertTrue(scheduler.idle)
            self.assertEqual(model.frames, [])
        finally:
            await scheduler.close()

    async def test_same_session_wakes_never_overlap_advance(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((lambda _frame: ModelDecision(content="done"),)),
            {},
            SequentialIds(),
        )
        original_advance = runtime.advance
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()
        calls = 0
        active = 0
        maximum_active = 0

        async def tracked_advance(session_id, **kwargs):
            nonlocal calls, active, maximum_active
            calls += 1
            call = calls
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if call == 1:
                    first_started.set()
                    await release_first.wait()
                return await original_advance(session_id, **kwargs)
            finally:
                active -= 1
                if call == 2:
                    second_finished.set()

        runtime.advance = tracked_advance
        scheduler = SessionScheduler(
            runtime,
            "session",
            control=AssistantControlPlane(()),
        )
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "hello",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake("session")
            await asyncio.wait_for(first_started.wait(), timeout=1)
            for _ in range(10):
                await scheduler.wake("session")
            await asyncio.sleep(0)
            self.assertEqual(maximum_active, 1)

            release_first.set()
            await asyncio.wait_for(second_finished.wait(), timeout=1)
            self.assertEqual(calls, 2)
            self.assertEqual(maximum_active, 1)
        finally:
            release_first.set()
            await scheduler.close()

    async def test_each_activation_calls_advance_once(self):
        second_finished = asyncio.Event()
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(content="first"),
                    lambda _frame: ModelDecision(content="second"),
                )
            ),
            {},
            SequentialIds(),
        )
        original_advance = runtime.advance
        advance_calls = 0

        async def tracked_advance(session_id, **kwargs):
            nonlocal advance_calls
            advance_calls += 1
            try:
                return await original_advance(session_id, **kwargs)
            finally:
                if advance_calls == 2:
                    second_finished.set()

        class ActivationCountingScheduler(SessionScheduler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.activations = 0

            def _start(self):
                self.activations += 1
                super()._start()

        runtime.advance = tracked_advance
        scheduler = ActivationCountingScheduler(
            runtime,
            "session",
            control=AssistantControlPlane(()),
        )
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

        try:
            await scheduler.wake("session")
            await asyncio.wait_for(second_finished.wait(), timeout=1)
            self.assertEqual(advance_calls, 2)
            self.assertEqual(scheduler.activations, 2)
        finally:
            await scheduler.close()

    async def test_activation_uses_advance_result_without_reading_state(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((lambda _frame: ModelDecision(content="done"),)),
            {},
            SequentialIds(),
        )
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "hello",
            delivery_id="user-1",
        )

        async def forbidden_state(_session_id):
            raise AssertionError("scheduler must not read state after advance")

        runtime.state = forbidden_state
        scheduler = SessionScheduler(
            runtime,
            "session",
            control=AssistantControlPlane(()),
        )
        try:
            should_continue = await scheduler._advance_once()
            self.assertFalse(should_continue)
        finally:
            await scheduler.close()
