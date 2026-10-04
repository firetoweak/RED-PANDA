from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from redpanda.runtime import (
    AgentRuntime,
    CommandPhase,
    DispatchAttemptStarted,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    SqliteJournal,
    ToolBinding,
)
from redpanda.runtime.journal.api import LeaseLostError, StepClaimRequest
from tests.assistant.test_runner import ScriptedDecisionMaker


class WorkerRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_attempt_can_renew_after_expiry_but_not_after_release(self):
        async def read(context, arguments):
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            for kind in ("memory", "sqlite"):
                with self.subTest(kind=kind):
                    now = [0.0]
                    journal = (
                        MemoryJournal(clock=lambda: now[0])
                        if kind == "memory"
                        else SqliteJournal(
                            Path(directory) / "journal.sqlite", clock=lambda: now[0]
                        )
                    )
                    runtime = AgentRuntime(
                        journal,
                        ScriptedDecisionMaker(
                            (
                                lambda _: ModelDecision(
                                    command_requests=(InvokeTool("read"),)
                                ),
                            )
                        ),
                        {"read": ToolBinding(read)},
                    )
                    await runtime.create_session("session")
                    await runtime.receive_user_message(
                        "session", "go", delivery_id="input"
                    )
                    try:
                        await runtime.advance("session")
                        attempt = next(
                            e.payload
                            for e in await journal.snapshot("session")
                            if isinstance(e.payload, DispatchAttemptStarted)
                        )
                        now[0] = 31
                        self.assertTrue(
                            await journal.renew_attempt(
                                attempt.attempt_id, attempt.claim_token, lease_seconds=1
                            )
                        )
                        await journal.release_attempt(
                            attempt.attempt_id, attempt.claim_token
                        )
                        self.assertFalse(
                            await journal.renew_attempt(
                                attempt.attempt_id, attempt.claim_token, lease_seconds=1
                            )
                        )
                    finally:
                        await runtime.dispatcher.close()

    async def test_recovery_preserves_unfinished_attempt_sibling_outcome_and_new_input(self):
        async def unfinished(context, arguments):
            await asyncio.Event().wait()

        async def finished(context, arguments):
            return "saved result"

        with tempfile.TemporaryDirectory() as directory:
            journal = SqliteJournal(Path(directory) / "journal.sqlite")
            runtime = AgentRuntime(
                journal,
                ScriptedDecisionMaker(
                    (
                        lambda _: ModelDecision(
                            command_requests=(InvokeTool("read"), InvokeTool("done")),
                        ),
                    )
                ),
                {"read": ToolBinding(unfinished), "done": ToolBinding(finished)},
            )
            await runtime.create_session("child")
            await runtime.receive_user_message("child", "go", delivery_id="first")
            await runtime.advance("child")
            async with asyncio.timeout(5):
                while (await runtime.state("child")).commands[
                    1
                ].phase is not CommandPhase.TERMINAL:
                    await asyncio.sleep(0.01)
            await runtime.receive_user_message(
                "child", "new input", delivery_id="second"
            )
            await runtime.dispatcher.close()
            before = await journal.snapshot("child")
            await journal.prepare_recovery("child")
            self.assertEqual(await journal.snapshot("child"), before)
            state = await runtime.state("child")
            self.assertEqual(state.commands[0].phase, CommandPhase.UNKNOWN)
            self.assertEqual(state.commands[1].phase, CommandPhase.TERMINAL)
            await journal.prepare_recovery("child")
            self.assertEqual(await journal.snapshot("child"), before)

    async def test_expiration_does_not_revoke_owner_but_takeover_does(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("memory", "sqlite"):
                with self.subTest(kind=kind):
                    now = [0.0]
                    journal = (
                        MemoryJournal(clock=lambda: now[0])
                        if kind == "memory"
                        else SqliteJournal(
                            Path(directory) / "journal.sqlite", clock=lambda: now[0]
                        )
                    )
                    runtime = AgentRuntime(
                        journal,
                        ScriptedDecisionMaker(
                            (lambda _: ModelDecision(content="done"),)
                        ),
                        {},
                    )
                    await runtime.create_session("session")
                    await runtime.receive_user_message(
                        "session", "go", delivery_id="input"
                    )
                    frame = runtime.projector.project(
                        "session", await journal.snapshot("session")
                    ).next_decision
                    request = StepClaimRequest(
                        "session",
                        frame.trigger_event.event_id,
                        frame.decision_cursor,
                        frame.basis_state_version,
                        frame.observed_journal_position,
                    )
                    first = await journal.acquire_step(
                        request, token="one", owner_id="old", lease_seconds=1
                    )
                    now[0] = 2
                    self.assertTrue(await journal.renew_step(first, lease_seconds=1))
                    now[0] = 4
                    second = await journal.acquire_step(
                        request, token="two", owner_id="new", lease_seconds=1
                    )
                    self.assertGreater(second.generation, first.generation)
                    self.assertFalse(await journal.renew_step(first, lease_seconds=1))
                    with self.assertRaises(LeaseLostError):
                        await runtime.step_runner.commit(frame, first)
                    runtime.step_runner._decision_maker = ScriptedDecisionMaker(
                        (lambda _: ModelDecision(content="done"),)
                    )
                    now[0] = 6
                    event = await runtime.step_runner.commit(frame, second)
                    self.assertEqual(event.payload.step.decision.content, "done")
