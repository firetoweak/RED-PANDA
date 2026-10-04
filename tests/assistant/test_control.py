from __future__ import annotations

import unittest
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from redpanda.assistant.artifacts import MemoryArtifactStore
from redpanda.assistant.context.projection import ModelContextProjector
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
    pending_approval_view,
    project_control,
    project_control_message,
    project_pending_approval,
)
from redpanda.assistant.decision import JournalBackedLlmDecisionMaker
from redpanda.assistant.delivery import deliver_binding
from redpanda.assistant.sessions import AssistantSessions
from redpanda.assistant.toolsets import ToolSurface
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from redpanda.runtime import (
    AgentRuntime,
    DomainFactCommitted,
    LeaseLostError,
    MemoryJournal,
    RuntimeStatus,
    StepCommitted,
)
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
        projector=projector,
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


class ConversationalControlTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_known_preparation_failure_is_distinct_fact(self):
        async def unavailable(_input: ProposalInput):
            return ControlPreparationFailure({
                "ok": False,
                "code": "SOURCE_UNAVAILABLE",
            })

        journal = MemoryJournal()
        control = AssistantControlPlane((_operation(unavailable),))
        runtime = _runtime(journal, control, llm=ControlLlm(control_calls=1))
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        await settle_session(runtime, SESSION_ID, control=control)

        facts = _facts(await journal.snapshot(SESSION_ID), CONTROL_PREPARATION_FAILED)
        self.assertEqual(len(facts), 1)
        self.assertTrue(facts[0].requests_decision)

    async def test_known_conclusion_is_distinct_fact(self):
        async def concluded(_input: ProposalInput):
            return {"ok": False, "code": "ALREADY_INSTALLED"}

        journal = MemoryJournal()
        control = AssistantControlPlane((_operation(concluded),))
        runtime = _runtime(journal, control)
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        await settle_session(runtime, SESSION_ID, control=control)

        facts = _facts(await journal.snapshot(SESSION_ID), CONTROL_CONCLUDED)
        self.assertEqual(len(facts), 1)
        self.assertTrue(facts[0].requests_decision)

    async def test_approval_records_decision_start_and_terminal_before_wake(self):
        handler = ApprovalHandler()
        journal, runtime, control, request = await _build_pending(handler=handler)
        scheduler = WakeRecorder()
        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            scheduler,
            control=control,
            management=SimpleNamespace(),
            catalog=EmptyCatalog(),
        )

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

    async def test_rejection_is_terminal_and_idempotent(self):
        journal, runtime, control, request = await _build_pending()
        scheduler = WakeRecorder()
        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            scheduler,
            control=control,
            management=SimpleNamespace(),
            catalog=EmptyCatalog(),
        )

        first = await sessions.resolve_control(SESSION_ID, request.id, approved=False)
        second = await sessions.resolve_control(SESSION_ID, request.id, approved=False)

        self.assertEqual(first, second)
        self.assertEqual(len(_facts(await journal.snapshot(SESSION_ID), CONTROL_REJECTED)), 1)
        self.assertIsNone(project_pending_approval(await journal.snapshot(SESSION_ID)))
        self.assertEqual(scheduler.woken, [SESSION_ID])

    async def test_unknown_execution_error_leaves_started_and_never_retries(self):
        handler = ApprovalHandler(fail=True)
        journal, runtime, control, request = await _build_pending(handler=handler)
        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            WakeRecorder(),
            control=control,
            management=SimpleNamespace(),
            catalog=EmptyCatalog(),
        )

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
        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            WakeRecorder(),
            control=restarted,
            management=SimpleNamespace(),
            catalog=EmptyCatalog(),
        )

        await sessions.recover_control(SESSION_ID)
        await sessions.recover_control(SESSION_ID)

        self.assertEqual(len(handler.payloads), 1)
        self.assertEqual(
            project_control(await journal.snapshot(SESSION_ID)).states[-1].phase,
            "succeeded",
        )

    async def test_conflicting_decision_is_rejected(self):
        _journal, runtime, control, request = await _build_pending()
        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            WakeRecorder(),
            control=control,
            management=SimpleNamespace(),
            catalog=EmptyCatalog(),
        )
        await sessions.resolve_control(SESSION_ID, request.id, approved=False)

        with self.assertRaises(ControlDecisionConflict):
            await sessions.resolve_control(SESSION_ID, request.id, approved=True)

    async def test_control_message_is_rebuilt_from_journal(self):
        journal, runtime, control, request = await _build_pending()
        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            WakeRecorder(),
            control=control,
            management=SimpleNamespace(),
            catalog=EmptyCatalog(),
        )
        await sessions.resolve_control(SESSION_ID, request.id, approved=True)

        restarted = AssistantSessions(
            runtime,
            ToolSurface(),
            WakeRecorder(),
            control=AssistantControlPlane((_operation(_proposal),)),
            management=SimpleNamespace(),
            catalog=EmptyCatalog(),
        )
        self.assertEqual((await restarted.view(SESSION_ID)).control_message, "安装完成")

    async def test_control_message_hides_after_the_next_user_message(self):
        journal, runtime, control, request = await _build_pending()
        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            WakeRecorder(),
            control=control,
            management=SimpleNamespace(),
            catalog=EmptyCatalog(),
        )
        await sessions.resolve_control(SESSION_ID, request.id, approved=True)
        self.assertEqual((await sessions.view(SESSION_ID)).control_message, "安装完成")

        await runtime.receive_user_message(SESSION_ID, "继续", delivery_id="next")

        self.assertIsNone((await sessions.view(SESSION_ID)).control_message)
        self.assertEqual(
            project_control(await journal.snapshot(SESSION_ID)).states[-1].phase,
            "succeeded",
        )

    async def test_projector_rejects_fact_for_another_request(self):
        journal, runtime, _control, request = await _build_pending()
        await runtime.receive_domain_fact(
            SESSION_ID,
            CONTROL_APPROVED,
            {"request_id": "control-request-wrong", "action": request.action},
            delivery_id="wrong:approved",
            source=CONTROL_SOURCE,
            requests_decision=False,
        )
        with self.assertRaisesRegex(ValueError, "没有对应请求"):
            project_control(await journal.snapshot(SESSION_ID))

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

    async def test_view_projects_pending_identity(self):
        journal, _runtime_value, _control, request = await _build_pending()
        view = pending_approval_view(await journal.snapshot(SESSION_ID))
        self.assertEqual(view.request_id, request.id)
