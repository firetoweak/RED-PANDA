from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from redpanda.runtime import (
    AgentRuntime,
    CommandAuthorized,
    CommandPhase,
    CommandRejected,
    DeliveryIdentity,
    DispatchAttemptStarted,
    DomainFactCommitted,
    EventDraft,
    InvokeTool,
    ModelDecision,
    RuntimeStatus,
    SqliteJournal,
    StepCommitted,
    ToolBinding,
    UserMessageReceived,
    diagnose_artifacts,
    replay,
)
from tests.fixtures.memory_journal import MemoryJournal
from redpanda.runtime.model import AuthorizationPolicy
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds


NOW = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)


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


def _requesting(name: str, content: str = "request", *follow_ups: str) -> ScriptedDecisionMaker:
    return ScriptedDecisionMaker((
        lambda _frame: ModelDecision(content=content, command_requests=(InvokeTool(name),)),
        *(lambda _frame, text=text: ModelDecision(content=text) for text in follow_ups),
    ))


async def _drain(runtime) -> None:
    while runtime.dispatcher.active_count:
        await asyncio.sleep(0)


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
        runtime = runtime_for(tool, _requesting("transfer"))

        await runtime.receive_user_message(self.SESSION_ID, "send money", delivery_id="ask-1")
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
        await _drain(runtime)

    async def test_rejected_command_cannot_be_granted_or_dispatched(self):
        tool = RecordingTool("publish", requires_authorization=True)
        model = _requesting("publish", "request publish", "publish rejected")
        runtime = runtime_for(tool, model)
        await runtime.receive_user_message(self.SESSION_ID, "publish this", delivery_id="ask-1")
        advance = await runtime.advance(self.SESSION_ID)
        step = advance.step
        command_id = step.commands[0].command_id

        events = await runtime.snapshot(self.SESSION_ID)
        issued_event_id = next(
            event.event_id
            for event in events
            if getattr(event.payload, "step", None) == step
        )
        rejected = await runtime.reject_command(self.SESSION_ID, command_id)
        self.assertIsInstance(rejected.payload, CommandRejected)
        self.assertEqual(rejected.causation_id, issued_event_id)
        self.assertIsNone(await runtime.grant_command(self.SESSION_ID, command_id))
        self.assertFalse(tool.started.is_set())

        state = await runtime.state(self.SESSION_ID)
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)
        self.assertEqual(state.next_trigger_event_id, rejected.event_id)
        self.assertNotIn(command_id, state.waiting_command_ids)

        follow_up = (await runtime.advance(self.SESSION_ID)).step
        self.assertEqual(follow_up.trigger_event_id, rejected.event_id)
        self.assertIsInstance(model.frames[1].trigger_event.payload, CommandRejected)
        self.assertFalse(tool.started.is_set())

    async def test_decision_content_is_not_delivered_user_message(self):
        tool = RecordingTool("deliver")
        runtime = runtime_for(tool, ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="任务完成",
                command_requests=(InvokeTool("deliver", (("text", "hello user"),)),),
            ),
        )))
        inbound = await runtime.receive_user_message(self.SESSION_ID, "say hello", delivery_id="ask-1")
        step = (await runtime.advance(self.SESSION_ID)).step
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        tool.release.set()
        await _drain(runtime)

        events = await runtime._journal.snapshot(self.SESSION_ID)
        self.assertEqual(step.decision.content, "任务完成")
        self.assertEqual(tool.executions, [{"text": "hello user"}])
        self.assertEqual(
            [e.event_id for e in events if isinstance(e.payload, UserMessageReceived)],
            [inbound.event_id],
        )

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
        runtime = runtime_for(tool, _requesting("sensitive"), journal=first)
        await runtime.receive_user_message(self.SESSION_ID, "do it", delivery_id="ask-1")
        step = (await runtime.advance(self.SESSION_ID)).step
        command_id = step.commands[0].command_id
        issued = (await first.snapshot(self.SESSION_ID))[-1]

        def grant(event_id):
            return EventDraft(
                event_id=event_id,
                session_id=self.SESSION_ID,
                payload=CommandAuthorized(command_id),
                occurred_at=NOW,
            )

        winners = await asyncio.gather(
            SqliteJournal(path).grant_command(grant("grant-a")),
            SqliteJournal(path).grant_command(grant("grant-b")),
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
        self.assertIsNone(await reopened.grant_command(grant("grant-c")))

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
                runtime = runtime_for(tool, _requesting("sensitive"), journal=journal)
                await runtime.receive_user_message(
                    self.SESSION_ID, "do it", delivery_id=f"ask-{name}"
                )
                step = (await runtime.advance(self.SESSION_ID)).step
                command_id = step.commands[0].command_id
                issued_event_id = next(
                    event.event_id
                    for event in await runtime.snapshot(self.SESSION_ID)
                    if isinstance(event.payload, StepCommitted)
                )

                rejected = await runtime.reject_command(self.SESSION_ID, command_id)

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
