from __future__ import annotations

import asyncio
from collections.abc import Awaitable
import unittest
from types import SimpleNamespace

from konden.assistant.control import AssistantControlPlane
from konden.assistant.decision import JournalBackedLlmDecisionMaker
from konden.assistant.context.projection import ModelContextProjector
from konden.assistant.delivery import PreviewEmitter, ensure_deliver
from konden.assistant.loop_guard import LoopGuard
from tests.fixtures.artifacts import MemoryArtifactGateway
from konden.assistant.subagent.subagent import (
    DELEGATE,
    INSTRUCT,
    LIST_SUBAGENTS,
    REPLY_FACT,
    RETURN_FACT,
    reply_arguments,
    instruction_arguments,
    instructed_result,
    RECLAIM,
    CLOSED_FACT,
    SubAgentHost,
    project_delegations,
    project_failed_delegations,
    project_pending,
    project_reclaimed,
    closed_arguments,
    return_data,
)
from konden.runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    CommandPhase,
    DomainFactCommitted,
    InvokeTool,
    ModelDecision,
    RuntimeStatus,
    ToolBinding,
)
from tests.fixtures.memory_journal import MemoryJournal
from konden.runtime.dispatcher import AttemptContext
from konden.runtime.state import DecisionFrame
from tests.assistant.test_runner import SequentialIds
from tests.session_scheduler import SettlingScheduler


def _attempt_context(session_id: str) -> AttemptContext:
    return AttemptContext(session_id, "command-x", "attempt-x", 1)


def _facts(events, fact_type: str):
    return [
        event.payload
        for event in events
        if isinstance(event.payload, DomainFactCommitted)
        and event.payload.fact_type == fact_type
    ]


def _pending(events) -> frozenset[str]:
    """父已委派但还没交回结论的子 Session。

    就地写出定义而不是调用生产函数：这些用例守的是不变量，换实现不该改测试。
    """

    return frozenset(project_delegations(events)) - project_reclaimed(events)



class _NoToolsets:
    def base_schemas(self):
        return []

    def toolset_schemas(self, _session_id, _state):
        return []


class _NoManagement:
    def schemas(self, _session_id, _state):
        return []

    def control_names(self, _session_id, _state):
        return frozenset()

    def catalog_instruction(self, _session_id, _state):
        return "没有管理面"


class _NoSkillTools:
    def schemas(self):
        return []


class _NoCliTools:
    def schemas(self):
        return []



class _Interleaving:
    """让 snapshot 真正交出控制权。

    脚本模型不会阻塞，两个子 Session 就总是一个跑完再跑另一个，测不出并发
    回收下的读改写。真实模型每次决策都要等网络，交错是常态。
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    async def snapshot(self, session_id: str):
        await asyncio.sleep(0)
        return await self._inner.snapshot(session_id)

    async def receive_domain_fact(self, *args, **kwargs):
        await asyncio.sleep(0)
        return await self._inner.receive_domain_fact(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _ParentChildDecisions:
    """父与子跑各自的脚本。子 Session 由 id 里的 /sub- 认出。"""

    def __init__(self, parent_scripts, child_scripts) -> None:
        self._parent_scripts = parent_scripts
        self._child_scripts = child_scripts
        self.parent_frames: list[DecisionFrame] = []
        self.child_frames: list[DecisionFrame] = []
        self._by_child: dict[str, list[DecisionFrame]] = {}

    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        session_id = frame.trigger_event.session_id
        if "/sub-" in session_id:
            # 每个子 Session 走自己的进度，并行委派时互不串位。
            own = self._by_child.setdefault(session_id, [])
            script = self._child_scripts[len(own)]
            own.append(frame)
            self.child_frames.append(frame)
        else:
            script = self._parent_scripts[len(self.parent_frames)]
            self.parent_frames.append(frame)
        decision = script(frame)
        if isinstance(decision, Awaitable):
            decision = await decision
        return ensure_deliver(decision, frame.trigger_event.event_id)


class LocalSessionRouter:
    """Route protocol tests to one scheduler per identity.

    Only these unit tests share an in-memory Runtime. Worker integration tests
    cover process and Journal isolation.
    """

    def __init__(self, runtime, **callbacks):
        self.runtime = runtime
        self.callbacks = callbacks
        self.schedulers = {}
        self.failure = None

    def record_failure(self, error):
        self.failure = error

    async def wake(self, session_id):
        if session_id not in self.schedulers:
            self.schedulers[session_id] = SettlingScheduler(
                self.runtime, session_id, **self.callbacks
            )
            self.runtime.dispatcher.connect(self.wake, self.record_failure)
        await self.schedulers[session_id].wake(session_id)

    async def join(self):
        while True:
            await asyncio.gather(*(
                scheduler.join() for scheduler in tuple(self.schedulers.values())
            ))
            if self.failure is not None:
                raise self.failure
            if all(scheduler.idle for scheduler in self.schedulers.values()):
                return

    async def close(self):
        await self.runtime.dispatcher.close()
        for scheduler in self.schedulers.values():
            await scheduler.close()


def local_transport(runtime, scheduler, host):
    """Test-only routing: production always routes between Worker processes."""

    async def route(operation, session_id, arguments):
        if operation == "create_child":
            await runtime.create_session(session_id)
            host._parents[session_id] = arguments["data"]["parent_session_id"]
            await runtime.receive_domain_fact(session_id, **arguments)
            await scheduler.wake(session_id)
            return {"ok": True}
        elif operation == "instruct_child":
            await runtime.receive_domain_fact(session_id, **instruction_arguments(arguments["parent_session_id"], arguments["command_id"], arguments["text"]))
            await scheduler.wake(session_id)
            return instructed_result(session_id)
        elif operation == "child_message":
            parent = host._parents[session_id]
            await runtime.receive_domain_fact(parent, **reply_arguments(session_id, arguments["command_id"], arguments["text"]))
            await scheduler.wake(parent)
            return {"ok": True}
        elif operation == "reclaim_child":
            await host._reclaim(session_id, arguments["parent_session_id"], failure=None, reason=arguments.get("reason"))
            return
        elif operation == "fact":
            await runtime.receive_domain_fact(session_id, **arguments)
            await host.refresh_activity(session_id)
            if await host.note_returned(session_id):
                return
        elif operation != "resume":
            raise AssertionError(operation)
        await scheduler.wake(session_id)

    return route


def _decision_maker(runtime, llm, host):
    return JournalBackedLlmDecisionMaker(
        runtime,
        llm,
        "test-model",
        compact_threshold_tokens=200000,
        environment="<environment />",
        surface=_NoToolsets(),
        skill_tools=_NoSkillTools(),
        cli_tools=_NoCliTools(),
        control=AssistantControlPlane(()),
        management=_NoManagement(),
        subagents=host,
        projector=ModelContextProjector(gateway=MemoryArtifactGateway()),
        loop_guard=LoopGuard(),
        preview=PreviewEmitter(),
    )



class SubAgentDelegationTest(unittest.IsolatedAsyncioTestCase):
    PARENT = "parent-session"

    def build(self, parent_scripts, child_scripts):
        host = SubAgentHost()
        model = _ParentChildDecisions(parent_scripts, child_scripts)
        runtime = AgentRuntime(MemoryJournal(), model, {
            **host.bindings(), **host.delivery_binding(lambda *_args: None),
        }, SequentialIds())
        router = LocalSessionRouter(runtime, on_failed=host.on_failed)
        host.attach(runtime, local_transport(runtime, router, host))
        return host, model, runtime, router

    async def test_ordinary_batches_reach_parent_and_only_parent_ends_collaboration(self):
        def child(frame):
            return frame.trigger_event.payload.data["child_session_id"]

        host, model, runtime, router = self.build((
            lambda _: ModelDecision(command_requests=(InvokeTool(DELEGATE, (("task", "分批交稿"),)),)),
            lambda f: ModelDecision(command_requests=(InvokeTool(INSTRUCT, (("child_session_id", child(f)), ("text", "补充第二批"))),)),
            lambda _: ModelDecision(content="第二批指令已发送"),
            lambda f: ModelDecision(command_requests=(InvokeTool(RECLAIM, (("child_session_id", child(f)), ("reason", "两批已验收"))),)),
            lambda _: ModelDecision(content="验收完成"),
        ), (
            lambda _: ModelDecision(content="第一批正文"),
            lambda _: ModelDecision(content="第二批正文"),
        ))
        await runtime.receive_user_message(self.PARENT, "开始", delivery_id="start")
        try:
            await router.wake(self.PARENT)
            await router.join()
            parent_events = await runtime.snapshot(self.PARENT)
            replies = _facts(parent_events, REPLY_FACT)
            self.assertEqual([p.data["text"] for p in replies], ["第一批正文", "第二批正文"])
            (child_id,) = project_delegations(parent_events)
            child_events = await runtime.snapshot(child_id)
            self.assertEqual(len(_facts(child_events, RETURN_FACT)), 1)
            self.assertEqual(_facts(child_events, RETURN_FACT)[0].data["reason"], "两批已验收")
            self.assertEqual(project_pending(parent_events), frozenset())
            self.assertFalse(_facts(parent_events, CLOSED_FACT)[0].requests_decision)
        finally:
            await router.close()

    async def test_reply_then_idle_is_open_and_survives_rehydrate(self):
        host, model, runtime, router = self.build((
            lambda _: ModelDecision(command_requests=(InvokeTool(DELEGATE, (("task", "写第一批后等待"),)),)),
            lambda _: ModelDecision(content="收到，稍后继续"),
        ), (lambda _: ModelDecision(content="第一批正文，等你回复"),))
        await runtime.receive_user_message(self.PARENT, "开始", delivery_id="start")
        try:
            await router.wake(self.PARENT)
            await router.join()
            events = await runtime.snapshot(self.PARENT)
            (child_id,) = project_pending(events)
            before = await runtime.snapshot(child_id)
            self.assertEqual(_facts(before, RETURN_FACT), [])
            self.assertEqual((await runtime.state(child_id)).waiting_for, ("external_fact",))
            restarted = SubAgentHost()
            restarted.attach(runtime, local_transport(runtime, router, restarted))
            self.assertEqual(await restarted.rehydrate(self.PARENT), (child_id,))
            await router.join()
            self.assertEqual(await runtime.snapshot(child_id), before)
            self.assertEqual(len(model.child_frames), 1)
        finally:
            await router.close()

    async def test_parallel_children_send_independent_replies_and_remain_open(self):
        _, _, runtime, router = self.build((
            lambda _: ModelDecision(command_requests=tuple(InvokeTool(DELEGATE, (("task", name),)) for name in ("A", "B"))),
            lambda _: ModelDecision(content="收到一位"),
            lambda _: ModelDecision(content="收到另一位"),
        ), (lambda f: ModelDecision(content=f.trigger_event.session_id),))
        await runtime.receive_user_message(self.PARENT, "并行调查", delivery_id="start")
        try:
            await router.wake(self.PARENT)
            await router.join()
            events = await runtime.snapshot(self.PARENT)
            replies = _facts(events, REPLY_FACT)
            self.assertEqual(len(replies), 2)
            self.assertEqual({r.data["text"] for r in replies}, set(project_pending(events)))
            self.assertEqual(_facts(events, CLOSED_FACT), [])
        finally:
            await router.close()


class SubAgentUnknownCreationRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_delegate_replays_only_its_stable_child_creation(self):
        blocked = asyncio.Event()

        async def interrupted(_context, _arguments):
            await blocked.wait()

        parent = "parent"
        runtime = AgentRuntime(
            MemoryJournal(),
            _ParentChildDecisions(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(
                            InvokeTool(DELEGATE, (("task", "查清事实"),)),
                        ),
                    ),
                ),
                (),
            ),
            {
                DELEGATE: ToolBinding(
                    interrupted,
                    decision_on_outcome=False,
                )
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(parent, "调查", delivery_id="user-1")
        await runtime.advance(parent)
        await asyncio.sleep(0)
        await runtime.dispatcher.close()
        state = await runtime.state(parent)
        self.assertEqual(state.commands[0].phase, CommandPhase.UNKNOWN)

        calls = []

        async def transport(operation, session_id, arguments):
            calls.append((operation, session_id, arguments))

        host = SubAgentHost()
        host.attach(runtime, transport)
        pending = await host.rehydrate(parent)

        intent = project_delegations(await runtime.snapshot(parent))[0]
        self.assertEqual(pending, (intent,))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "create_child")
        self.assertEqual(calls[0][1], intent)
        self.assertEqual(calls[0][2]["data"]["task"], "查清事实")
        self.assertEqual(
            calls[0][2]["data"]["delegate_command_id"],
            state.commands[0].command.command_id,
        )
        recovered = await runtime.state(parent)
        self.assertEqual(recovered.commands[0].phase, CommandPhase.TERMINAL)
        self.assertEqual(
            recovered.commands[0].outcome.value,
            {"ok": True, "code": "DELEGATED", "data": {"child_session_id": intent}},
        )
        await host.rehydrate(parent)
        self.assertEqual([call[0] for call in calls], ["create_child", "resume"])
        self.assertEqual(
            sum(isinstance(event.payload, CommandOutcomeReceived)
                for event in await runtime.snapshot(parent)),
            1,
        )
        await runtime.receive_domain_fact(
            parent,
            **closed_arguments(
                intent,
                return_data(intent, failure="中断"),
            ),
        )
        self.assertEqual((await runtime.state(parent)).status, RuntimeStatus.RUNNABLE)

    async def test_known_delegate_failure_is_not_left_pending(self):
        async def reject(_context, _arguments):
            return {
                "ok": False,
                "code": "DELEGATION_NOT_ALLOWED",
                "data": {"session_id": "parent"},
                "error": "not allowed",
            }

        runtime = AgentRuntime(
            MemoryJournal(),
            _ParentChildDecisions(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(
                            InvokeTool(DELEGATE, (("task", "查清事实"),)),
                        ),
                    ),
                ),
                (),
            ),
            {DELEGATE: ToolBinding(reject, decision_on_outcome=False)},
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime, "parent")
        await runtime.receive_user_message("parent", "调查", delivery_id="user-1")
        try:
            await scheduler.wake("parent")
            await scheduler.join()
            events = await runtime.snapshot("parent")
            delegated = project_delegations(events)
            self.assertEqual(len(delegated), 1)
            self.assertEqual(
                project_failed_delegations(events),
                frozenset(delegated),
            )
            self.assertEqual(project_pending(events), frozenset())
        finally:
            await scheduler.close()


class SubAgentPolicyTest(unittest.IsolatedAsyncioTestCase):
    def test_child_uses_ordinary_speech_without_special_reply_or_finish_tools(self):
        host = SubAgentHost()
        host._parents["child"] = "parent"
        self.assertEqual(host.schemas("child"), [])
        self.assertTrue({DELEGATE, INSTRUCT, RECLAIM, LIST_SUBAGENTS}.isdisjoint(host.tool_names("child")))

    async def test_instruction_rejects_invalid_arguments_and_unowned_child(self):
        from unittest.mock import AsyncMock

        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("parent")
        transport = AsyncMock()
        host = SubAgentHost()
        host.attach(runtime, transport)
        for arguments in (
            {"child_session_id": "other/sub-child", "text": " "},
            {"child_session_id": "other/sub-child", "text": 123},
            {"child_session_id": "other/sub-child", "text": "换方向", "interrupt": False},
        ):
            with self.subTest(arguments=arguments):
                result = await host._instruct_command(_attempt_context("parent"), arguments)
                self.assertEqual(result["code"], "INVALID_ARGUMENT")
        result = await host._instruct_command(
            _attempt_context("parent"), {"child_session_id": "other/sub-child", "text": "换方向"},
        )
        self.assertEqual(result["code"], "UNKNOWN_CHILD")
        transport.assert_not_awaited()

    async def test_external_schemas_bypass_builtin_name_filter(self):
        from konden.assistant.toolsets import ToolSurface
        from tests.assistant.test_toolsets import FakeEchoProvider

        surface = ToolSurface(providers=(FakeEchoProvider(),), base_schemas=[
            {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object"}}}
            for name in ("write_file", "execute_command")
        ])
        runtime = AgentRuntime(MemoryJournal(), None, {})
        surface.attach(runtime)
        surface.apply_catalog("child", surface.registry_descriptors())
        await surface.load("child", "demo")
        host = SubAgentHost()
        host._parents["child"] = "parent"
        maker = _decision_maker(runtime, None, host)
        maker._surface = surface
        schemas, _ = maker.schemas_for(SimpleNamespace(session_id="child"), ())
        names = {schema["function"]["name"] for schema in schemas}
        self.assertIn("demo_ping", names)
        self.assertIn("load_toolset", names)
        self.assertIn("write_file", names)
        self.assertIn("execute_command", names)

    def test_unknown_session_has_no_policy(self):
        host = SubAgentHost()
        self.assertIsNone(host.tool_names("plain-session"))
        self.assertIsNone(host.system_prompt("plain-session"))
        self.assertEqual(
            [schema["function"]["name"] for schema in host.schemas("plain")],
            [DELEGATE, INSTRUCT, LIST_SUBAGENTS, RECLAIM],
        )
