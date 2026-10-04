from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from redpanda.runtime import (
    AgentRuntime,
    CommandAuthorized,
    CommandPhase,
    CommandRejected,
    DecisionFrame,
    DeliveryIdentity,
    DispatchAttemptStarted,
    DomainFactCommitted,
    EventDraft,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    SqliteJournal,
    StepCommitted,
    ToolBinding,
    UserMessageReceived,
    diagnose_artifacts,
    replay,
)
from redpanda.runtime.events import EventPayload
from redpanda.runtime.model import AuthorizationPolicy


NOW = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)
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


class RecordingTool:
    def __init__(
        self,
        name: str,
        *,
        requires_authorization: bool | AuthorizationPolicy = False,
    ) -> None:
        self.name = name
        self.requires_authorization = requires_authorization
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.executions: list[Mapping[str, object]] = []

    def binding(self) -> dict[str, ToolBinding]:
        return {
            self.name: ToolBinding(
                self._handler,
                requires_authorization=self.requires_authorization,
            )
        }

    async def _handler(self, _context, arguments: Mapping[str, object]) -> object:
        self.executions.append(arguments)
        self.started.set()
        await self.release.wait()
        return f"{self.name}-result"


def runtime_for(
    tool: RecordingTool,
    model: ScriptedDecisionMaker,
    journal=None,
):
    return AgentRuntime(
        journal or MemoryJournal(),
        model,
        tool.binding(),
        SequentialIds(),
    )


class AgentRuntimeBoundarySliceTest(unittest.IsolatedAsyncioTestCase):
    SESSION_ID = "boundary-session"

    async def test_stale_advance_position_does_not_claim_or_consume_decision(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent), tempfile.TemporaryDirectory() as root:
                journal = SqliteJournal(Path(root) / "journal.sqlite") if persistent else MemoryJournal()
                model = ScriptedDecisionMaker((lambda _: ModelDecision(content="done"),))
                runtime = runtime_for(RecordingTool("read"), model, journal)
                await runtime.receive_user_message(self.SESSION_ID, "first", delivery_id="first")
                checked = await runtime.snapshot(self.SESSION_ID)
                await runtime.receive_user_message(self.SESSION_ID, "later", delivery_id="later")
                current = await runtime.snapshot(self.SESSION_ID)

                deferred = await runtime.advance(
                    self.SESSION_ID, expected_journal_position=checked[-1].sequence
                )
                self.assertIsNone(deferred.step)
                self.assertEqual(deferred.status, RuntimeStatus.RUNNABLE)
                self.assertEqual(model.frames, [])
                self.assertEqual(await runtime.snapshot(self.SESSION_ID), current)

                committed = await runtime.advance(
                    self.SESSION_ID, expected_journal_position=current[-1].sequence
                )
                self.assertIsNotNone(committed.step)
                self.assertEqual(model.frames[0].observed_journal_position, current[-1].sequence)

    async def test_unauthorized_command_is_not_claimed_until_granted(self):
        tool = RecordingTool("transfer", requires_authorization=True)
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="request transfer",
                        command_requests=(InvokeTool("transfer"),),
                    ),
                )
            ),
        )

        await runtime.receive_user_message(
            self.SESSION_ID,
            "send money",
            delivery_id="ask-1",
        )
        advance = await runtime.advance(self.SESSION_ID)
        step = advance.step
        command_id = step.commands[0].command_id
        state = await runtime.state(self.SESSION_ID)

        self.assertTrue(step.commands[0].requires_authorization)
        self.assertEqual(advance.status, RuntimeStatus.WAITING)
        self.assertIsNone(state.command(command_id).dispatch_eligible_by_event_id)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(state.waiting_for, (f"authorization:{command_id}",))
        self.assertFalse(tool.started.is_set())

        denied = await runtime.dispatcher.start_pending(self.SESSION_ID)
        self.assertEqual(denied.started_command_ids, ())
        bypass = await runtime.dispatcher._journal.start_attempt(
            EventDraft(
                event_id="forged-attempt",
                session_id=self.SESSION_ID,
                payload=DispatchAttemptStarted(
                    "forged-attempt",
                    command_id,
                    1,
                    "forged-claim",
                    "attacker",
                ),
                occurred_at=NOW,
                causation_id=None,
            )
        )
        self.assertIsNone(bypass)

        granted = await runtime.grant_command(self.SESSION_ID, command_id)
        self.assertIsInstance(granted.payload, CommandAuthorized)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        dispatched = (await runtime.state(self.SESSION_ID)).command(command_id)
        self.assertEqual(dispatched.phase, CommandPhase.UNKNOWN)
        self.assertEqual(len(dispatched.attempts), 1)
        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)

    async def test_rejected_command_cannot_be_granted_or_dispatched(self):
        tool = RecordingTool("publish", requires_authorization=True)
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    content="request publish",
                    command_requests=(InvokeTool("publish"),),
                ),
                lambda _frame: ModelDecision(content="publish rejected"),
            )
        )
        runtime = runtime_for(tool, model)
        await runtime.receive_user_message(
            self.SESSION_ID,
            "publish this",
            delivery_id="ask-1",
        )
        advance = await runtime.advance(self.SESSION_ID)
        step = advance.step
        command_id = step.commands[0].command_id

        events = await runtime.snapshot(self.SESSION_ID)
        issued_event_id = next(
            event.event_id
            for event in events
            if getattr(event.payload, "step", None) == step
        )
        snapshot_calls = 0
        original_snapshot = runtime._journal.snapshot

        async def tracked_snapshot(session_id):
            nonlocal snapshot_calls
            snapshot_calls += 1
            return await original_snapshot(session_id)

        runtime._journal.snapshot = tracked_snapshot

        rejected = await runtime.reject_command(self.SESSION_ID, command_id)
        self.assertIsInstance(rejected.payload, CommandRejected)
        self.assertEqual(rejected.causation_id, issued_event_id)
        self.assertEqual(snapshot_calls, 0)
        self.assertIsNone(await runtime.grant_command(self.SESSION_ID, command_id))
        self.assertFalse(tool.started.is_set())

        state = await runtime.state(self.SESSION_ID)
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)
        self.assertEqual(state.next_trigger_event_id, rejected.event_id)
        self.assertEqual(
            state.command(command_id).authorization_rejected_by_event_id,
            rejected.event_id,
        )
        self.assertNotIn(command_id, state.waiting_command_ids)

        follow_up = (await runtime.advance(self.SESSION_ID)).step
        self.assertEqual(follow_up.trigger_event_id, rejected.event_id)
        self.assertIsInstance(
            model.frames[1].trigger_event.payload,
            CommandRejected,
        )
        self.assertFalse(tool.started.is_set())

    async def test_decision_content_is_not_delivered_user_message(self):
        tool = RecordingTool("deliver")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="任务完成",
                        command_requests=(
                            InvokeTool("deliver", (("text", "hello user"),)),
                        ),
                    ),
                )
            ),
        )
        inbound = await runtime.receive_user_message(
            self.SESSION_ID,
            "say hello",
            delivery_id="ask-1",
        )
        step = (await runtime.advance(self.SESSION_ID)).step
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)

        events = await runtime._journal.snapshot(self.SESSION_ID)
        self.assertEqual(step.decision.content, "任务完成")
        self.assertEqual(tool.executions, [{"text": "hello user"}])
        self.assertEqual(
            [
                event.event_id
                for event in events
                if isinstance(event.payload, UserMessageReceived)
            ],
            [inbound.event_id],
        )

    async def test_dispatch_attempt_does_not_change_advance_status(self):
        tool = RecordingTool("slow")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="run",
                        command_requests=(InvokeTool("slow"),),
                    ),
                )
            ),
        )
        await runtime.receive_user_message(
            self.SESSION_ID,
            "run it",
            delivery_id="ask-1",
        )

        advance = await runtime.advance(self.SESSION_ID)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        state_after_dispatch = await runtime.state(self.SESSION_ID)

        self.assertEqual(advance.status, state_after_dispatch.status)
        self.assertEqual(
            len(
                state_after_dispatch.command(
                    advance.step.commands[0].command_id
                ).attempts
            ),
            1,
        )
        tool.release.set()
        while runtime.dispatcher.active_count:
            await asyncio.sleep(0)

    async def test_preview_tokens_cannot_enter_the_journal(self):
        with self.assertRaises(TypeError):
            EventDraft(
                event_id="preview-1",
                session_id=self.SESSION_ID,
                payload="streaming token",
                occurred_at=NOW,
            )
        payload_names = {
            getattr(item, "__name__", str(item)) for item in EventPayload.__args__
        }
        self.assertNotIn("SessionPreviewReceived", payload_names)
        self.assertIn("StepCommitted", payload_names)
        self.assertIn("CommandAuthorized", payload_names)

    async def test_missing_artifact_degrades_replay_without_substitution(self):
        digest = "a" * 64
        journal = MemoryJournal()
        await journal.accept_delivery(
            EventDraft(
                event_id="message-1",
                session_id=self.SESSION_ID,
                payload=UserMessageReceived("read this"),
                occurred_at=NOW,
                artifact_refs=(digest,),
                delivery=DeliveryIdentity("user", "ask-1"),
            )
        )
        events = await journal.snapshot(self.SESSION_ID)
        uninspected = diagnose_artifacts(events)
        rebuilt = replay(self.SESSION_ID, events)
        inspected_empty = diagnose_artifacts(events, available_refs=())

        self.assertEqual(uninspected.refs, (digest,))
        self.assertEqual(uninspected.missing, ())
        self.assertFalse(uninspected.inspected)
        self.assertFalse(uninspected.complete)
        self.assertEqual(rebuilt.trace.entries[0].kind, "UserMessageReceived")
        self.assertFalse(rebuilt.artifacts.inspected)
        self.assertEqual(rebuilt.artifacts.missing, ())
        self.assertTrue(inspected_empty.inspected)
        self.assertEqual(inspected_empty.missing, (digest,))
        self.assertFalse(inspected_empty.complete)
        self.assertTrue(diagnose_artifacts(events, available_refs=(digest,)).complete)

    async def test_sqlite_grant_is_single_winner_and_survives_restart(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "boundary.db"
        first = SqliteJournal(path)
        tool = RecordingTool("sensitive", requires_authorization=True)
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="need grant",
                        command_requests=(InvokeTool("sensitive"),),
                    ),
                )
            ),
            journal=first,
        )
        await runtime.receive_user_message(
            self.SESSION_ID,
            "do it",
            delivery_id="ask-1",
        )
        step = (await runtime.advance(self.SESSION_ID)).step
        command_id = step.commands[0].command_id
        issued = (await first.snapshot(self.SESSION_ID))[-1]

        journal_a = SqliteJournal(path)
        journal_b = SqliteJournal(path)
        draft_a = EventDraft(
            event_id="grant-a",
            session_id=self.SESSION_ID,
            payload=CommandAuthorized(command_id),
            occurred_at=NOW,
        )
        draft_b = EventDraft(
            event_id="grant-b",
            session_id=self.SESSION_ID,
            payload=CommandAuthorized(command_id),
            occurred_at=NOW,
        )
        winners = await asyncio.gather(
            journal_a.grant_command(draft_a),
            journal_b.grant_command(draft_b),
        )
        self.assertEqual(sum(event is not None for event in winners), 1)
        self.assertEqual(
            next(event.causation_id for event in winners if event is not None),
            issued.event_id,
        )

        reopened = SqliteJournal(path)
        events = await reopened.snapshot(self.SESSION_ID)
        rebuilt = replay(self.SESSION_ID, events)
        self.assertIsNotNone(
            rebuilt.state.command(command_id).dispatch_eligible_by_event_id
        )
        self.assertIsNone(
            await reopened.grant_command(
                EventDraft(
                    event_id="grant-c",
                    session_id=self.SESSION_ID,
                    payload=CommandAuthorized(command_id),
                    occurred_at=NOW,
                )
            )
        )

    async def test_authorization_reject_contract_matches_across_journals(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        journals = (
            ("memory", MemoryJournal()),
            ("sqlite", SqliteJournal(Path(temporary.name) / "reject.db")),
        )

        for name, journal in journals:
            with self.subTest(journal=name):
                tool = RecordingTool("sensitive", requires_authorization=True)
                runtime = runtime_for(
                    tool,
                    ScriptedDecisionMaker(
                        (
                            lambda _frame: ModelDecision(
                                content="need authorization",
                                command_requests=(InvokeTool("sensitive"),),
                            ),
                        )
                    ),
                    journal=journal,
                )
                await runtime.receive_user_message(
                    self.SESSION_ID,
                    "do it",
                    delivery_id=f"ask-{name}",
                )
                step = (await runtime.advance(self.SESSION_ID)).step
                command_id = step.commands[0].command_id
                issued_event_id = next(
                    event.event_id
                    for event in await runtime.snapshot(self.SESSION_ID)
                    if isinstance(event.payload, StepCommitted)
                )

                rejected = await runtime.reject_command(
                    self.SESSION_ID,
                    command_id,
                )

                self.assertEqual(rejected.causation_id, issued_event_id)
                self.assertIsNone(
                    await runtime.reject_command(self.SESSION_ID, command_id)
                )
                with self.assertRaises(KeyError):
                    await runtime.grant_command(self.SESSION_ID, "missing")
                with self.assertRaises(KeyError):
                    await runtime.reject_command(self.SESSION_ID, "missing")

    async def test_domain_fact_requires_delivery_across_journals(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        journals = (
            ("memory", MemoryJournal()),
            ("sqlite", SqliteJournal(Path(temporary.name) / "fact.db")),
        )

        for name, journal in journals:
            with self.subTest(journal=name):
                with self.assertRaises(ValueError):
                    await journal.append(
                        EventDraft(
                            event_id=f"bare-fact-{name}",
                            session_id=self.SESSION_ID,
                            payload=DomainFactCommitted("probe", {"a": 1}),
                            occurred_at=NOW,
                        )
                    )
                accepted = await journal.accept_delivery(
                    EventDraft(
                        event_id=f"delivered-fact-{name}",
                        session_id=self.SESSION_ID,
                        payload=DomainFactCommitted("probe", {"a": 1}),
                        occurred_at=NOW,
                        delivery=DeliveryIdentity("probe", "probe-1"),
                    )
                )

                self.assertTrue(accepted.inserted)
                self.assertEqual(
                    accepted.event.delivery,
                    DeliveryIdentity("probe", "probe-1"),
                )
