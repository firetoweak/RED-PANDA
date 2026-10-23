from __future__ import annotations

import json
import asyncio
import unittest
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from tests.fixtures.artifacts import MemoryArtifactGateway, MemoryArtifactStore
from redpanda.assistant.context.projection import ModelContextProjector
from tests.fixtures.projection import project_chat_messages
from redpanda.assistant.control import (
    CONTROL_APPROVED,
    CONTROL_CONCLUDED,
    CONTROL_EXECUTION_STARTED,
    CONTROL_FAILED,
    CONTROL_PREPARATION_FAILED,
    CONTROL_PROPOSED,
    CONTROL_REJECTED,
    CONTROL_REQUEST_METADATA,
    CONTROL_SOURCE,
    CONTROL_SUCCEEDED,
    AssistantControlPlane,
    ControlDecisionConflict,
    project_control,
    project_control_message,
    project_pending_approval,
)
from redpanda.assistant.decision import JournalBackedLlmDecisionMaker
from redpanda.assistant.delivery import PreviewEmitter, deliver_binding
from redpanda.assistant.loop_guard import LoopGuard
from redpanda.assistant.sessions import AssistantSessions
from redpanda.assistant.toolsets import ToolSurface
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from redpanda.runtime import (
    AgentRuntime,
    DomainFactCommitted,
    LeaseLostError,
    RuntimeStatus,
    StepCommitted,
)
from tests.fixtures.memory_journal import MemoryJournal
from redpanda.runtime.json_values import thaw_value
from redpanda.tools.control import (
    ControlApprovalExecution,
    ControlApprovalProposal,
    ControlOperation,
    ControlPreparationFailure,
)
from redpanda.tools.spec import PydanticParameters, ToolSpec
from tests.session_scheduler import settle_session


SESSION_ID = "control-session"
PROPOSAL_NAME = "propose_test_control"


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class ApprovalHandler:
    action = "test.install"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.payloads: list[object] = []

    async def execute(self, payload):
        self.payloads.append(payload)
        if self.fail:
            raise RuntimeError("approval execution failed")
        return ControlApprovalExecution(True, "安装完成")


class ControlLlm:
    def __init__(self, *, control_calls: int = 1) -> None:
        self.control_calls = control_calls
        self.calls = 0

    async def chat(self, _messages, _model, *, tools=None):
        self.calls += 1
        calls = ()
        if self.calls <= self.control_calls:
            names = {tool["function"]["name"] for tool in tools}
            if PROPOSAL_NAME not in names:
                raise AssertionError("control schema missing")
            calls = (
                ToolCall("control-1", PROPOSAL_NAME, '{"value":"frozen"}'),
            )
        return LLMCallResult(
            LLMResponse(content="done", calls=calls),
            LLMUsage(input_tokens=1, output_tokens=1),
        )


class EmptyTools:
    def schemas(self):
        return []


class OpenControlManagement:
    def schemas(self, _session_id, _state):
        return []

    def control_names(self, _session_id, _state):
        return frozenset({PROPOSAL_NAME})

    def catalog_instruction(self, _session_id, _state):
        return "test management"


class FailOnceJournal(MemoryJournal):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def commit_step(self, lease, draft):
        if self.mode:
            mode, self.mode = self.mode, ""
            if mode == "lease":
                raise LeaseLostError(lease.token)
            raise RuntimeError("commit failed")
        return await super().commit_step(lease, draft)


class SaveFailingStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    def save(self, content):
        if self.fail:
            self.fail = False
            raise RuntimeError("decision evidence save failed")
        return super().save(content)


class SaveFailingGateway:
    def __init__(self) -> None:
        self.store = SaveFailingStore()

    def for_session(self, _session_id):
        return self.store


class WakeRecorder:
    def __init__(self) -> None:
        self.woken: list[str] = []

    async def wake(self, session_id: str) -> None:
        self.woken.append(session_id)


class EmptyCatalog:
    def rehydrate(self, _session_id, _events):
        return None

    async def sync(self, _runtime, _session_id):
        return None


def _operation(propose, handler: ApprovalHandler | None = None) -> ControlOperation:
    return ControlOperation(
        "test",
        ToolSpec(
            PROPOSAL_NAME,
            "提交测试控制方案。",
            PydanticParameters(ProposalInput),
            propose,
            control_boundary=True,
            exclusive_batch=True,
        ),
        ApprovalHandler() if handler is None else handler,
    )


def _decision_maker(journal, llm, control, *, projector=None):
    return JournalBackedLlmDecisionMaker(
        journal,
        llm,
        "test-model",
        compact_threshold_tokens=200000,
        environment="<environment />",
        surface=ToolSurface(),
        skill_tools=EmptyTools(),
        cli_tools=EmptyTools(),
        control=control,
        management=OpenControlManagement(),
        projector=(
            ModelContextProjector(gateway=MemoryArtifactGateway())
            if projector is None
            else projector
        ),
        loop_guard=LoopGuard(),
        preview=PreviewEmitter(),
    )


def _runtime(journal, control, *, llm=None, projector=None):
    return AgentRuntime(
        journal,
        _decision_maker(
            journal,
            ControlLlm() if llm is None else llm,
            control,
            projector=projector,
        ),
        deliver_binding(lambda _session_id, _output_id, _text: None),
    )


def _facts(events, fact_type: str) -> list[DomainFactCommitted]:
    return [
        event.payload
        for event in events
        if isinstance(event.payload, DomainFactCommitted)
        and event.payload.fact_type == fact_type
    ]


async def _proposal(_input: ProposalInput):
    return ControlApprovalProposal(
        "test.install",
        {"value": _input.value},
        "安装 frozen",
        "测试风险",
    )


async def _build_pending(*, handler=None):
    journal = MemoryJournal()
    control = AssistantControlPlane((_operation(_proposal, handler),))
    runtime = _runtime(journal, control)
    await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
    await settle_session(runtime, SESSION_ID, control=control)
    request = project_pending_approval(await journal.snapshot(SESSION_ID))
    assert request is not None
    return journal, runtime, control, request


def _sessions(runtime, control, scheduler=None) -> AssistantSessions:
    return AssistantSessions(
        runtime,
        ToolSurface(),
        WakeRecorder() if scheduler is None else scheduler,
        control=control,
        management=SimpleNamespace(),
        catalog=EmptyCatalog(),
    )


def _recovery_config(workspace):
    from redpanda.assistant.host import worker
    from tests.fixtures.session_worker import config_for

    class Handler(ApprovalHandler):
        async def execute(self, payload):
            with (workspace / "control-executions.txt").open("a", encoding="utf-8") as target:
                target.write(payload["value"] + "\n")
            return await super().execute(payload)

    build = worker.build_assistant_assembly

    async def assembly(*args, **kwargs):
        result = await build(*args, **kwargs)
        result.sessions._control = AssistantControlPlane((_operation(_proposal, Handler()),))
        return result

    worker.build_assistant_assembly = assembly
    return config_for(workspace)


@pytest.mark.process
class StartupControlRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_resumes_committed_control_work_with_no_automatic_retry(self):
        from redpanda.assistant.host.session_store import SessionStore
        from redpanda.assistant.host.supervisor import HostSupervisor
        from redpanda.assistant.sessions import session_view
        from redpanda.paths import RedPandaHome
        from redpanda.runtime import SqliteJournal, replay
        from tests.assistant.test_supervisor import until
        from tests.fixtures.session_worker import ProcessLlm

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = RedPandaHome(root / "home")
            store = SessionStore(home.runtime_sessions_root)

            def host():
                return HostSupervisor(
                    store, partial(_recovery_config, root), home, lambda *_: None, llm=ProcessLlm(root),
                )

            owner = host()
            before = {}
            try:
                for phase in ("requested", "proposed", "approved", "execution_started"):
                    await store.create(phase, directory=root)
                    journal = SqliteJournal(store.require(phase))
                    control = AssistantControlPlane((_operation(_proposal),))
                    runtime = _runtime(journal, control)
                    await runtime.receive_user_message(phase, "安装它", delivery_id="input")
                    await runtime.advance(phase)
                    await asyncio.gather(*runtime.dispatcher._tasks.values())

                    async def commit(outcome):
                        await runtime.receive_domain_fact(
                            phase, outcome.fact_type, dict(outcome.data), source=CONTROL_SOURCE,
                            delivery_id=outcome.delivery_id, requests_decision=outcome.requests_decision,
                        )

                    if phase != "requested":
                        await commit(await control.prepare_pending(await journal.snapshot(phase)))
                    if phase in {"approved", "execution_started"}:
                        request = project_pending_approval(await journal.snapshot(phase))
                        await commit(control.decision_outcome(request, approved=True))
                        if phase == "execution_started":
                            await commit(control.execution_started_outcome(request))
                    before[phase] = await journal.snapshot(phase)
                    self.assertFalse(session_view(replay(phase, before[phase]).state).should_wake)
                    await runtime.dispatcher.close()
            finally:
                await owner.close()

            for _restart in range(2):
                owner = host()
                try:
                    await asyncio.wait_for(owner.resume_unfinished_sessions(), 30)
                    await until(lambda: not owner.workers and not owner.watchers)
                    self.assertTrue(owner.failures.empty())
                    self.assertEqual((root / "control-executions.txt").read_text(encoding="utf-8"), "frozen\n")
                    for phase in before:
                        events = await SqliteJournal(store.require(phase)).snapshot(phase)
                        state = project_control(events)
                        if phase == "approved":
                            self.assertEqual(state.states[-1].phase, "succeeded")
                            self.assertEqual(len(_facts(events, CONTROL_EXECUTION_STARTED)), 1)
                            self.assertEqual(len(_facts(events, CONTROL_SUCCEEDED)), 1)
                        elif phase == "requested":
                            self.assertEqual(state.active.phase, "proposed")
                            self.assertEqual(len(_facts(events, CONTROL_PROPOSED)), 1)
                        else:
                            self.assertEqual(events, before[phase])
                finally:
                    await owner.close()


class ConversationalControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_sees_proposal_status_without_internal_payload(self):
        journal, runtime, _control, request = await _build_pending()
        events = await journal.snapshot(SESSION_ID)
        proposal = _facts(events, CONTROL_PROPOSED)[0]
        self.assertEqual(thaw_value(proposal.data["payload"]), {"value": "frozen"})
        self.assertEqual(dict(request.payload), {"value": "frozen"})
        messages = project_chat_messages(
            events, runtime.projector.project_visible(SESSION_ID, events), "sys",
        )
        fact = next(
            json.loads(message["content"])["data"]
            for message in messages
            if message["role"] == "user" and CONTROL_PROPOSED in message["content"]
        )
        self.assertNotIn("payload", fact)
        self.assertEqual(fact["summary"], request.summary)
        self.assertIn("尚未执行", fact["status"])

    async def test_request_is_committed_with_step_before_preparation(self):
        observed_step = []
        journal = MemoryJournal()

        async def propose(input_data: ProposalInput):
            events = await journal.snapshot(SESSION_ID)
            observed_step.append(any(isinstance(e.payload, StepCommitted) for e in events))
            return await _proposal(input_data)

        control = AssistantControlPlane((_operation(propose),))
        runtime = _runtime(journal, control)
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        await settle_session(runtime, SESSION_ID, control=control)

        events = await journal.snapshot(SESSION_ID)
        step = next(
            event.payload for event in events if isinstance(event.payload, StepCommitted)
        )
        metadata = thaw_value(step.decision_metadata)
        intent = metadata[CONTROL_REQUEST_METADATA]
        request = project_pending_approval(events)
        self.assertEqual(observed_step, [True])
        self.assertEqual(request.id, intent["request_id"])
        self.assertEqual(intent["arguments"], {"value": "frozen"})
        self.assertEqual([c.effect.name for c in step.step.commands], ["deliver"])

    async def test_fresh_control_plane_recovers_preparation_from_step(self):
        journal = MemoryJournal()
        operation = _operation(_proposal)
        original = AssistantControlPlane((operation,))
        runtime = _runtime(journal, original)
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")

        advanced = await runtime.advance(SESSION_ID)
        self.assertIsNotNone(advanced.step)
        events = await journal.snapshot(SESSION_ID)
        self.assertIsNone(project_pending_approval(events))

        restarted = AssistantControlPlane((operation,))
        outcome = await restarted.prepare_pending(events)
        self.assertEqual(outcome.fact_type, CONTROL_PROPOSED)
        intent = project_control(events).active.intent
        self.assertEqual(outcome.data["request_id"], intent.request_id)

    async def test_unknown_preparation_error_bubbles_without_fact(self):
        async def broken(_input: ProposalInput):
            raise RuntimeError("probe bug")

        journal = MemoryJournal()
        control = AssistantControlPlane((_operation(broken),))
        runtime = _runtime(journal, control)
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        await runtime.advance(SESSION_ID)

        with self.assertRaisesRegex(RuntimeError, "probe bug"):
            await control.prepare_pending(await journal.snapshot(SESSION_ID))
        self.assertEqual(
            _facts(await journal.snapshot(SESSION_ID), CONTROL_PREPARATION_FAILED),
            [],
        )

    async def test_known_preparation_outcomes_are_distinct_facts(self):
        async def unavailable(_input: ProposalInput):
            return ControlPreparationFailure({"ok": False, "code": "SOURCE_UNAVAILABLE"})

        async def concluded(_input: ProposalInput):
            return {"ok": False, "code": "ALREADY_INSTALLED"}

        for propose, fact_type in (
            (unavailable, CONTROL_PREPARATION_FAILED),
            (concluded, CONTROL_CONCLUDED),
        ):
            with self.subTest(fact_type=fact_type):
                journal = MemoryJournal()
                control = AssistantControlPlane((_operation(propose),))
                runtime = _runtime(journal, control)
                await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
                await settle_session(runtime, SESSION_ID, control=control)

                facts = _facts(await journal.snapshot(SESSION_ID), fact_type)
                self.assertEqual(len(facts), 1)
                self.assertTrue(facts[0].requests_decision)

    async def test_approval_records_decision_start_and_terminal_before_wake(self):
        handler = ApprovalHandler()
        journal, runtime, control, request = await _build_pending(handler=handler)
        scheduler = WakeRecorder()
        sessions = _sessions(runtime, control, scheduler)

        message = await sessions.resolve_control(
            SESSION_ID,
            request.id,
            approved=True,
        )

        self.assertEqual(message, "安装完成")
        self.assertEqual(len(handler.payloads), 1)
        events = await journal.snapshot(SESSION_ID)
        phases = [
            event.payload.fact_type
            for event in events
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type.startswith(f"{CONTROL_SOURCE}.")
        ]
        self.assertEqual(
            phases,
            [CONTROL_PROPOSED, CONTROL_APPROVED, CONTROL_EXECUTION_STARTED, CONTROL_SUCCEEDED],
        )
        self.assertEqual(scheduler.woken, [SESSION_ID])
        self.assertEqual(project_control(events).states[-1].phase, "succeeded")
        messages = project_chat_messages(
            events, runtime.projector.project_visible(SESSION_ID, events), "sys",
        )
        model_facts = [
            json.loads(message["content"])["data"]
            for message in messages
            if message["role"] == "user"
            and any(kind in message["content"] for kind in (CONTROL_EXECUTION_STARTED, CONTROL_SUCCEEDED))
        ]
        self.assertEqual(len(model_facts), 2)
        self.assertTrue(all("execution_id" not in fact for fact in model_facts))
        self.assertEqual(model_facts[-1]["message"], "安装完成")

    async def test_rejection_is_terminal_and_idempotent(self):
        journal, runtime, control, request = await _build_pending()
        scheduler = WakeRecorder()
        sessions = _sessions(runtime, control, scheduler)

        first = await sessions.resolve_control(SESSION_ID, request.id, approved=False)
        second = await sessions.resolve_control(SESSION_ID, request.id, approved=False)

        self.assertEqual(first, second)
        self.assertEqual(len(_facts(await journal.snapshot(SESSION_ID), CONTROL_REJECTED)), 1)
        self.assertIsNone(project_pending_approval(await journal.snapshot(SESSION_ID)))
        self.assertEqual(scheduler.woken, [SESSION_ID])

    async def test_unknown_execution_error_leaves_started_and_never_retries(self):
        handler = ApprovalHandler(fail=True)
        journal, runtime, control, request = await _build_pending(handler=handler)
        sessions = _sessions(runtime, control)

        with self.assertRaisesRegex(RuntimeError, "approval execution failed"):
            await sessions.resolve_control(SESSION_ID, request.id, approved=True)

        events = await journal.snapshot(SESSION_ID)
        self.assertEqual(project_control(events).active.phase, "execution_started")
        self.assertEqual(_facts(events, CONTROL_FAILED), [])
        self.assertIn("执行结果未知", project_control_message(events))

        duplicate = await sessions.resolve_control(SESSION_ID, request.id, approved=True)
        self.assertIn("执行结果未知", duplicate)
        self.assertEqual(len(handler.payloads), 1)

    async def test_approved_without_started_is_recovered_once(self):
        handler = ApprovalHandler()
        journal, runtime, control, request = await _build_pending(handler=handler)
        await runtime.receive_domain_fact(
            SESSION_ID,
            CONTROL_APPROVED,
            {"request_id": request.id, "action": request.action},
            delivery_id=f"{request.id}:approved",
            source=CONTROL_SOURCE,
            requests_decision=False,
        )
        restarted = AssistantControlPlane((_operation(_proposal, handler),))
        sessions = _sessions(runtime, restarted)

        await sessions.recover_control(SESSION_ID)
        await sessions.recover_control(SESSION_ID)

        self.assertEqual(len(handler.payloads), 1)
        self.assertEqual(
            project_control(await journal.snapshot(SESSION_ID)).states[-1].phase,
            "succeeded",
        )

    async def test_conflicting_decision_is_rejected(self):
        _journal, runtime, control, request = await _build_pending()
        sessions = _sessions(runtime, control)
        await sessions.resolve_control(SESSION_ID, request.id, approved=False)

        with self.assertRaises(ControlDecisionConflict):
            await sessions.resolve_control(SESSION_ID, request.id, approved=True)

    async def test_control_message_rebuilds_from_journal_until_next_user_message(self):
        journal, runtime, control, request = await _build_pending()
        await _sessions(runtime, control).resolve_control(
            SESSION_ID, request.id, approved=True,
        )
        restarted = _sessions(runtime, AssistantControlPlane((_operation(_proposal),)))
        self.assertEqual((await restarted.view(SESSION_ID)).control_message, "安装完成")

        await runtime.receive_user_message(SESSION_ID, "继续", delivery_id="next")

        self.assertIsNone((await restarted.view(SESSION_ID)).control_message)
        self.assertEqual(
            project_control(await journal.snapshot(SESSION_ID)).states[-1].phase,
            "succeeded",
        )

    async def _assert_retry_clears_uncommitted_stage(
        self,
        journal,
        *,
        projector=None,
        expected_error=None,
    ):
        control = AssistantControlPlane((_operation(_proposal),))
        llm = ControlLlm(control_calls=2)
        runtime = _runtime(journal, control, llm=llm, projector=projector)
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        if expected_error is None:
            first = await runtime.advance(SESSION_ID)
            self.assertIsNone(first.step)
            self.assertIs(first.status, RuntimeStatus.RUNNABLE)
        else:
            with self.assertRaisesRegex(RuntimeError, expected_error):
                await runtime.advance(SESSION_ID)
        await settle_session(runtime, SESSION_ID, control=control)
        self.assertIsNotNone(project_pending_approval(await journal.snapshot(SESSION_ID)))
        self.assertGreaterEqual(llm.calls, 2)

    async def test_artifact_failure_does_not_leak_stage_into_retry(self):
        await self._assert_retry_clears_uncommitted_stage(
            MemoryJournal(),
            projector=ModelContextProjector(gateway=SaveFailingGateway()),
            expected_error="evidence save failed",
        )

    async def test_commit_failure_does_not_leak_stage_into_retry(self):
        await self._assert_retry_clears_uncommitted_stage(
            FailOnceJournal("commit"),
            expected_error="commit failed",
        )

    async def test_lease_loss_does_not_leak_stage_into_retry(self):
        await self._assert_retry_clears_uncommitted_stage(FailOnceJournal("lease"))
