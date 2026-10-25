from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from konden.runtime import (
    AgentRuntime, DecisionCancelled, InvokeTool, ModelDecision, RuntimeStatus,
    SqliteJournal, StateProjector, StepClaimRequest, StepCommitted, StepContinuationCancelled,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.runtime.test_boundary_slice import RecordingTool, runtime_for
from tests.runtime.test_semantic_slice import _drain


class UserInputBatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "session.sqlite"
        self.journal = SqliteJournal(self.path)

    def runtime(self, *scripts, tools=None):
        runtime = AgentRuntime(self.journal, ScriptedDecisionMaker(scripts),
                               tools or {}, SequentialIds())
        self.addAsyncCleanup(runtime.dispatcher.close)
        return runtime

    async def user(self, runtime, text):
        return await runtime.receive_user_message("session", text, delivery_id=text)

    async def test_result_arriving_during_human_reply_is_delivered_after_the_frozen_reply(self):
        tool = RecordingTool("work")
        replying, finish_reply = asyncio.Event(), asyncio.Event()

        async def reply(frame):
            self.assertIsNone(frame.state.commands[0].outcome)
            replying.set()
            await finish_reply.wait()
            return ModelDecision(content="answer the new question")

        runtime = self.runtime(lambda _: ModelDecision(command_requests=(InvokeTool("work"),)),
                               reply, lambda _: ModelDecision(content="report the finished work"), tools=tool.binding())
        await self.user(runtime, "start")
        await runtime.advance("session")
        await tool.started.wait()
        new = await self.user(runtime, "new question")
        advancing = asyncio.create_task(runtime.advance("session"))
        await replying.wait()
        tool.release.set()
        await _drain(runtime)
        finish_reply.set()
        answer = await advancing
        self.assertEqual(answer.step.trigger_event_ids, (new.event_id,))
        completed = await runtime.advance("session")
        self.assertEqual(completed.step.decision.content, "report the finished work")
        self.assertEqual(len(tool.executions), 1)
        self.assertIsNone((await runtime.advance("session")).step)

    async def test_handled_human_message_does_not_hide_a_later_recovery_of_unfinished_work(self):
        tool = RecordingTool("work")
        runtime = self.runtime(lambda _: ModelDecision(command_requests=(InvokeTool("work"),)),
                               lambda _: ModelDecision(content="answer"), tools=tool.binding())
        await self.user(runtime, "start")
        await runtime.advance("session")
        await tool.started.wait()
        await self.user(runtime, "new question")
        await runtime.advance("session")
        await runtime.dispatcher.close()
        await self.journal.prepare_recovery("session")
        events = await self.journal.snapshot("session")
        frame = StateProjector().project("session", events).next_decision
        self.assertIsNotNone(frame)
        self.assertEqual(frame.state.commands[0].phase.value, "unknown")
        self.assertIsNone(frame.state.commands[0].outcome)
        self.assertEqual(len(tool.executions), 1)
        await self.journal.prepare_recovery("session")
        self.assertEqual(await self.journal.snapshot("session"), events)

    async def test_human_batch_overtakes_request_and_keeps_earlier_outcome(self):
        tool = RecordingTool("work")
        model = ScriptedDecisionMaker((
            lambda _: ModelDecision(command_requests=(InvokeTool("work"),)),
            lambda _: ModelDecision(content="release 2.1"),
            lambda _: ModelDecision(content="answer B"),
        ))
        runtime = runtime_for(tool, model, journal=self.journal)
        self.addAsyncCleanup(runtime.dispatcher.close)
        await self.user(runtime, "start")
        first = await runtime.advance("session")
        await tool.started.wait()
        request = await runtime.receive_domain_fact(
            "session", "group.attention", {"text": "B"},
            delivery_id="B", source="group", requests_decision=True,
        )
        # The result precedes the human batch and must still accompany its Step.
        tool.release.set()
        await _drain(runtime)
        one = await self.user(runtime, "release 2.0")
        two = await self.user(runtime, "change to 2.1")
        note = await runtime.receive_domain_fact(
            "session", "group.message", {"text": "ordinary speech"},
            delivery_id="note", source="group",
        )
        result = await runtime.advance("session")
        frame = model.frames[1]
        self.assertEqual(result.step.trigger_event_ids, (one.event_id, two.event_id))
        self.assertNotIn(request.event_id, frame.state.visible_event_ids)
        self.assertIn(note.event_id, frame.state.visible_event_ids)
        self.assertIsNotNone(frame.state.command(first.step.commands[0].command_id).outcome)
        self.assertEqual(frame.state.user_messages, ("start", "release 2.0", "change to 2.1"))
        self.assertEqual((await runtime.advance("session")).step.trigger_event_ids, (request.event_id,))
        self.assertIsNone((await runtime.advance("session")).step)
        # Replay consumes the recorded sets, even if today's selection policy changes.
        with patch.object(StateProjector, "_requires_decision", return_value=False):
            state = StateProjector().project("session", await SqliteJournal(self.path).snapshot("session")).state
        self.assertEqual(state.decision_cursor, 3)
        self.assertEqual([item.step.trigger_event_ids for item in state.steps],
                         [(model.frames[0].trigger_event.event_id,),
                          (one.event_id, two.event_id), (request.event_id,)])

    async def test_failed_attempt_consumes_nothing_and_retry_extends_batch(self):
        def fail(_):
            raise LookupError("model unavailable")

        runtime = self.runtime(fail, lambda _: ModelDecision(content="done"))
        one = await self.user(runtime, "one")
        with self.assertRaisesRegex(LookupError, "model unavailable"):
            await runtime.advance("session")
        self.assertFalse(any(isinstance(event.payload, StepCommitted)
                             for event in await runtime.snapshot("session")))
        two = await self.user(runtime, "two")
        result = await runtime.advance("session")
        self.assertEqual(result.step.trigger_event_ids, (one.event_id, two.event_id))
        self.assertEqual((await runtime.state("session")).decision_cursor, 1)

    async def test_cancel_frozen_batch_preserves_input_arriving_during_model_call(self):
        started = asyncio.Event()

        async def block(_):
            started.set()
            await asyncio.Event().wait()

        runtime = self.runtime(block)
        one = await self.user(runtime, "one")
        two = await self.user(runtime, "two")
        advancing = asyncio.create_task(runtime.advance("session"))
        await started.wait()
        later = await self.user(runtime, "later")
        cancellations = await runtime.cancel_turn("session")
        await advancing
        self.assertEqual(len(cancellations), 1)
        self.assertIsInstance(cancellations[0].payload, DecisionCancelled)
        self.assertEqual(cancellations[0].payload.trigger_event_ids, (one.event_id, two.event_id))
        events = await SqliteJournal(self.path).snapshot("session")
        frame = StateProjector().project("session", events).next_decision
        self.assertEqual(frame.trigger_event_ids, (later.event_id,))
        self.assertIn(cancellations[0].event_id, frame.state.visible_event_ids)
        self.assertEqual(frame.state.steps, ())

    async def test_failed_commit_releases_the_batch_for_a_new_attempt(self):
        runtime = self.runtime(lambda _: ModelDecision(content="first"),
                               lambda _: ModelDecision(content="retry"))
        one = await self.user(runtime, "one")
        with patch.object(self.journal, "commit_step", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                await runtime.advance("session")
        self.assertEqual((await runtime.state("session")).decision_cursor, 0)
        two = await self.user(runtime, "two")
        self.assertEqual((await runtime.advance("session")).step.trigger_event_ids,
                         (one.event_id, two.event_id))

    async def test_cancel_before_model_call_consumes_selected_batch(self):
        runtime = self.runtime()
        one = await self.user(runtime, "one")
        two = await self.user(runtime, "two")
        (cancelled,) = await runtime.cancel_turn("session")
        self.assertEqual(cancelled.payload.trigger_event_ids, (one.event_id, two.event_id))
        self.assertEqual((await runtime.state("session")).status, RuntimeStatus.WAITING)

    async def test_commit_winning_cancellation_preserves_later_input(self):
        thinking = asyncio.Event()
        release = asyncio.Event()
        committed = asyncio.Event()

        class CommitWinsJournal(SqliteJournal):
            async def commit_step(self, lease, draft):
                event = await super().commit_step(lease, draft)
                committed.set()
                return event

            async def cancel_decision(self, draft):
                release.set()
                await committed.wait()
                return await super().cancel_decision(draft)

        async def decide(_):
            thinking.set()
            await release.wait()
            return ModelDecision(content="committed")

        self.journal = CommitWinsJournal(self.path)
        runtime = self.runtime(decide)
        await self.user(runtime, "one")
        await self.user(runtime, "two")
        advancing = asyncio.create_task(runtime.advance("session"))
        await thinking.wait()
        later = await self.user(runtime, "later")
        (cancellation,) = await runtime.cancel_turn("session")
        await advancing
        self.assertIsInstance(cancellation.payload, StepContinuationCancelled)
        events = await runtime.snapshot("session")
        self.assertEqual(sum(isinstance(event.payload, StepCommitted) for event in events), 1)
        self.assertFalse(any(isinstance(event.payload, DecisionCancelled) for event in events))
        self.assertEqual(runtime.projector.project("session", events).next_decision.trigger_event_ids,
                         (later.event_id,))

    async def test_later_correction_joins_the_whole_batch_without_waiting_for_running_tool(self):
        tool = RecordingTool("work")
        runtime = self.runtime(lambda _: ModelDecision(command_requests=(InvokeTool("work"),)),
                               tools=tool.binding())
        # A recorded decision can have selected B while an earlier human input was
        # still pending. Replay must honor that selection, not today's priority.
        await runtime.receive_domain_fact("session", "request", {}, delivery_id="B",
                                          source="peer", requests_decision=True)
        frame = runtime.projector.project("session", await runtime.snapshot("session")).next_decision
        one = await self.user(runtime, "release 2.0")
        frame = replace(frame, observed_journal_position=one.sequence)
        lease = await self.journal.acquire_step(
            StepClaimRequest("session", frame.trigger_event_ids, frame.decision_cursor,
                             frame.basis_state_version, frame.observed_journal_position),
            token="claim", owner_id="worker", lease_seconds=30,
        )
        await runtime.step_runner.commit(frame, lease)
        await runtime.dispatcher.start_pending("session")
        await tool.started.wait()
        await self.user(runtime, "change to 2.1")
        frame = runtime.projector.project("session", await runtime.snapshot("session")).next_decision
        self.assertEqual([event.payload.content for event in frame.trigger_events],
                         ["release 2.0", "change to 2.1"])
        self.assertIsNone(frame.state.commands[0].outcome)
        tool.release.set()
        await _drain(runtime)
        next_frame = runtime.projector.project("session", await runtime.snapshot("session")).next_decision
        self.assertEqual([event.payload.content for event in next_frame.trigger_events],
                         ["release 2.0", "change to 2.1"])
