from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from konden.runtime import (
    AgentRuntime,
    DecisionCancelled,
    InvokeTool,
    ModelDecision,
    RuntimeStatus,
    SqliteJournal,
    StepContinuationCancelled,
    ToolBinding,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds


class DecisionCancellationTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_new_reply_does_not_resume_from_the_older_tool_result(self):
        from tests.runtime.test_boundary_slice import RecordingTool, runtime_for

        tool = RecordingTool("work")
        replying = asyncio.Event()

        async def reply(_):
            replying.set()
            await asyncio.Event().wait()

        runtime = runtime_for(tool, ScriptedDecisionMaker((
            lambda _: ModelDecision(command_requests=(InvokeTool("work"),)), reply,
        )))
        self.addAsyncCleanup(runtime.dispatcher.close)
        await runtime.receive_user_message("session", "start", delivery_id="first")
        await runtime.advance("session")
        await tool.started.wait()
        new = await runtime.receive_user_message("session", "new question", delivery_id="new")
        advancing = asyncio.create_task(runtime.advance("session"))
        await replying.wait()
        cancellations = await runtime.cancel_turn("session")
        await advancing
        self.assertEqual(cancellations[0].payload.trigger_event_ids, (new.event_id,))
        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)
        self.assertIsNone((await runtime.advance("session")).step)
        self.assertEqual(len(tool.executions), 1)

    async def test_cancelled_decision_consumes_its_trigger_durably(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def block(_frame):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.sqlite3"
            runtime = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker((block,)),
                {},
                SequentialIds(),
            )
            await runtime.receive_user_message(
                "session",
                "first",
                delivery_id="delivery-1",
            )
            advancing = asyncio.create_task(runtime.advance("session"))
            await started.wait()

            cancellations = await runtime.cancel_turn("session")
            result = await advancing

            self.assertEqual(len(cancellations), 1)
            event = cancellations[0]
            self.assertIsInstance(event.payload, DecisionCancelled)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(result.status, RuntimeStatus.WAITING)

            restarted = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(()),
                {},
            )
            state = await restarted.state("session")
            self.assertEqual(state.status, RuntimeStatus.WAITING)
            self.assertEqual(state.decision_cursor, 1)
            self.assertIsNone(state.next_trigger_event_id)

            await restarted.receive_user_message(
                "session",
                "second",
                delivery_id="delivery-2",
            )
            frame = restarted.projector.project(
                "session",
                await restarted.snapshot("session"),
            ).next_decision
            self.assertEqual(frame.trigger_event.payload.content, "second")
            self.assertEqual(frame.state.user_messages, ("first", "second"))

    async def test_cancel_turn_preserves_outcome_without_continuing_step(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def block(_context, _arguments):
            started.set()
            await release.wait()
            return "done"

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "session.sqlite3"
        runtime = AgentRuntime(
            SqliteJournal(path),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(InvokeTool("block"),),
                    ),
                )
            ),
            {"block": ToolBinding(block)},
            SequentialIds(),
        )
        await runtime.receive_user_message(
            "session",
            "run",
            delivery_id="delivery",
        )
        await runtime.advance("session")
        await started.wait()

        cancellations = await runtime.cancel_turn("session")
        self.assertEqual(len(cancellations), 1)
        self.assertIsInstance(
            cancellations[0].payload,
            StepContinuationCancelled,
        )
        self.assertEqual(runtime.dispatcher.active_count, 1)

        release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0.01)

        state = await runtime.state("session")
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(state.commands[0].outcome.value, "done")
        self.assertEqual(len(state.steps), 1)

        restarted = AgentRuntime(
            SqliteJournal(path),
            ScriptedDecisionMaker(()),
            {},
        )
        self.assertEqual(
            (await restarted.state("session")).status,
            RuntimeStatus.WAITING,
        )

        await runtime.receive_user_message(
            "session",
            "continue",
            delivery_id="delivery-2",
        )
        frame = runtime.projector.project(
            "session",
            await runtime.snapshot("session"),
        ).next_decision
        self.assertEqual(frame.trigger_event.payload.content, "continue")
        self.assertEqual(frame.state.commands[0].outcome.value, "done")


if __name__ == "__main__":
    unittest.main()
