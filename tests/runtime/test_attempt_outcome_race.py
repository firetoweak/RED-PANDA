from __future__ import annotations

import asyncio
import unittest

from redpanda.runtime import (
    AgentRuntime,
    CommandOutcome,
    CommandOutcomeReceived,
    DispatchAttemptStarted,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    OutcomeStatus,
    ToolBinding,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds


class AttemptOutcomeRaceTest(unittest.IsolatedAsyncioTestCase):
    async def _start(self, release: asyncio.Event):
        started = asyncio.Event()

        async def block(_context, _arguments):
            started.set()
            await release.wait()
            return {"ok": True, "code": "FROM_HANDLER"}

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(command_requests=(InvokeTool("block"),)),
            )),
            {"block": ToolBinding(block)},
            SequentialIds(),
        )
        await runtime.receive_user_message("session", "run", delivery_id="delivery")
        advancing = asyncio.create_task(runtime.advance("session"))
        await asyncio.wait_for(started.wait(), timeout=1)
        await advancing
        dispatch = next(
            event
            for event in await runtime.snapshot("session")
            if isinstance(event.payload, DispatchAttemptStarted)
        )
        return runtime, dispatch

    async def _accept(self, runtime, dispatch, code: str, ok):
        await runtime.dispatcher.accept_outcome(
            "session",
            dispatch.payload.command_id,
            dispatch.payload.attempt_id,
            dispatch.event_id,
            CommandOutcome(
                OutcomeStatus.SUCCEEDED,
                value={
                    "ok": ok,
                    "code": code,
                    "data": None,
                    "error": None,
                    "hint": None,
                },
            ),
        )

    def _codes(self, events) -> list[str]:
        return [
            event.payload.outcome.value["code"]
            for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
        ]

    async def test_first_terminal_wins_when_a_later_outcome_differs(self):
        release = asyncio.Event()
        runtime, dispatch = await self._start(release)
        await self._accept(runtime, dispatch, "NATURAL", True)
        await self._accept(runtime, dispatch, "COMMAND_INTERRUPTED", None)
        release.set()
        for _ in range(50):
            if runtime.dispatcher.active_count == 0:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(runtime.dispatcher.active_count, 0)
        self.assertEqual(
            self._codes(await runtime.snapshot("session")),
            ["NATURAL"],
        )

    async def test_interrupt_written_first_is_not_replaced_by_a_natural_outcome(self):
        release = asyncio.Event()
        runtime, dispatch = await self._start(release)
        await self._accept(runtime, dispatch, "COMMAND_INTERRUPTED", None)
        await self._accept(runtime, dispatch, "NATURAL", True)
        release.set()
        for _ in range(50):
            if runtime.dispatcher.active_count == 0:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(
            self._codes(await runtime.snapshot("session")),
            ["COMMAND_INTERRUPTED"],
        )
