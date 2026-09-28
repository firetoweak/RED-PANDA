from __future__ import annotations

import asyncio
from collections.abc import Awaitable
import unittest
from types import SimpleNamespace

from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.decision import JournalBackedLlmDecisionMaker
from helperme.assistant.delivery import deliver_binding
from helperme.assistant.subagent.subagent import (
    DELEGATE,
    CHILD_BUILTIN_TOOL_NAMES,
    RECLAIM,
    REPORT,
    REPORT_FACT,
    TASK_FACT,
    DelegateIntent,
    SubAgentHost,
    project_delegations,
    project_failed_delegations,
    project_parent,
    project_pending,
    project_reclaimed,
    project_report,
    report_arguments,
    return_data,
    task_fact_arguments,
)
from helperme.llm.api import LLMProviderError
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage
from helperme.runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    CommandPhase,
    DomainFactCommitted,
    InvokeTool,
    LeaseLostError,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    ToolBinding,
)
from helperme.runtime.dispatcher import AttemptContext
from helperme.runtime.state import DecisionFrame
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


class _RecordingLlm:
    """记下每次决策实际发给模型的 system 提示。

    「还差谁」投影得再准，没拼进提示模型也看不见，所以断言落在这里而不是
    投影的返回值上。
    """

    def __init__(self) -> None:
        self.system_prompts: list[str] = []

    async def chat(self, messages, _model, *, tools=None):
        self.system_prompts.append(messages[0]["content"])
        return LLMCallResult(
            LLMResponse(content="ok", calls=()),
            LLMUsage(input_tokens=1, output_tokens=1),
        )


class _NoToolsets:
    def base_schemas(self):
        return []

    def toolset_schemas(self, _session_id, _state):
        return []

    def catalog_instruction(self, _session_id, _state):
        return "没有 Toolset"


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


def _visible_to(events, frame: DecisionFrame):
    """一次决策实际看得见的事实，口径与 `decide()` 的 frame 边界一致。"""

    return tuple(
        event for event in events if event.sequence <= frame.observed_journal_position
    )


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
            return await decision
        return decision


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
        elif operation == "reclaim_child":
            await host.cancel(session_id, arguments)
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


class SubAgentDelegationTest(unittest.IsolatedAsyncioTestCase):
    PARENT = "parent-session"

    def _build(
        self,
        parent_scripts,
        child_scripts,
        *,
        delivered=None,
        activity=None,
        interleave=False,
    ):
        host = SubAgentHost(
            None
            if activity is None
            else lambda session_id, active: activity.append((session_id, active))
        )
        model = _ParentChildDecisions(parent_scripts, child_scripts)
        bindings = dict(host.bindings())
        if delivered is not None:
            bindings.update(
                deliver_binding(
                    host.routed_sink(
                        lambda session_id, _output_id, text: delivered.append((session_id, text))
                    )
                )
            )
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            bindings,
            SequentialIds(),
        )
        def _surface(session_id, text):
            if delivered is None or host.is_subagent(session_id):
                return None
            delivered.append((session_id, text))
            return None

        scheduler = LocalSessionRouter(
            runtime,
            session_failed=_surface,
            on_quiesced=host.on_quiesced,
            on_failed=host.on_failed,
        )
        host.attach(
            _Interleaving(runtime) if interleave else runtime,
            local_transport(
                _Interleaving(runtime) if interleave else runtime, scheduler, host
            ),
        )
        return host, model, runtime, scheduler

    async def _child_session_id(self, runtime) -> str:
        reports = _facts(await runtime.snapshot(self.PARENT), REPORT_FACT)
        return reports[0].data["child_session_id"]

    async def test_single_delegation_returns_its_report_to_the_parent(self):
        delivered: list[tuple[str, str]] = []
        activity: list[tuple[str, bool]] = []
        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查清 A 的调用点"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="收到子 Agent 结论"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(
                            REPORT,
                            (("summary", "A 有三个调用点，见 x.py:10"),),
                        ),
                    ),
                ),
            ),
            delivered=delivered,
            activity=activity,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "帮我查 A",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
            await asyncio.sleep(0)

            parent_events = await runtime.snapshot(self.PARENT)
            reports = _facts(parent_events, REPORT_FACT)
            self.assertEqual(len(reports), 1)
            self.assertIs(reports[0].requests_decision, True)
            self.assertEqual(
                reports[0].data["summary"],
                "A 有三个调用点，见 x.py:10",
            )
            self.assertIs(reports[0].data["reported"], True)

            child_session_id = reports[0].data["child_session_id"]
            self.assertTrue(host.is_subagent(child_session_id))
            self.assertEqual(host.parent_of(child_session_id), self.PARENT)

            # 子的任务事实落在子自己的 Journal，父只拿到结论。
            child_events = await runtime.snapshot(child_session_id)
            tasks = _facts(child_events, TASK_FACT)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].data["task"], "查清 A 的调用点")
            self.assertEqual(_facts(parent_events, TASK_FACT), [])

            # 父因为这条事实醒来，做了第二次决策。
            self.assertEqual(len(model.parent_frames), 2)
            self.assertEqual(
                model.parent_frames[1].trigger_event.payload.fact_type,
                REPORT_FACT,
            )
            self.assertEqual(
                (await runtime.state(self.PARENT)).status,
                RuntimeStatus.WAITING,
            )
            self.assertEqual(activity[0], (self.PARENT, True))
            self.assertEqual(activity[-1], (self.PARENT, False))
        finally:
            await scheduler.close()

    async def test_one_step_can_open_several_children_at_once(self):
        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    content="我同时让两个子 Agent 去查",
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查 A"),)),
                        InvokeTool(DELEGATE, (("task", "查 B"),)),
                    ),
                ),
                # 两条结论各自到达还是挤在一次决策里取决于时序，
                # 父可能被唤醒一次也可能两次，脚本给足即可。
                lambda _frame: ModelDecision(content="收到一条"),
                lambda _frame: ModelDecision(content="收到另一条"),
            ),
            child_scripts=(
                lambda frame: ModelDecision(
                    command_requests=(
                        InvokeTool(
                            REPORT,
                            (
                                (
                                    "summary",
                                    f"结论来自 {frame.trigger_event.session_id}",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            interleave=True,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "同时查 A 和 B",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            parent_events = await runtime.snapshot(self.PARENT)
            reports = _facts(parent_events, REPORT_FACT)
            children = [report.data["child_session_id"] for report in reports]

            self.assertEqual(len(reports), 2)
            self.assertEqual(len(set(children)), 2)
            for report in reports:
                self.assertIs(report.data["reported"], True)
                self.assertEqual(
                    report.data["summary"],
                    f"结论来自 {report.data['child_session_id']}",
                )
            # 两条结论都送到了，父不会漏看其中一条。
            self.assertEqual(
                set(project_delegations(parent_events)),
                set(children),
            )
            self.assertEqual(project_reclaimed(parent_events), frozenset(children))
            # 全部交回之后，父不再欠任何一条结论。
            self.assertEqual(_pending(parent_events), frozenset())
        finally:
            await scheduler.close()

    async def test_parent_never_sees_an_empty_pending_set_too_early(self):
        """还有子没交回结论时，父那一帧看到的待回收集合不能是空的。

        「还差人没回来」由父在决策时从自己已冻结的事实里投影，而
        `project_delegations` 必须读 Step 中已经原子提交的 delegate Commands。
        一个极快的子即使在兄弟 Outcome 落库前回收，父也必须已经知道全部兄弟。
        """

        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查 A"),)),
                        InvokeTool(DELEGATE, (("task", "查 B"),)),
                        InvokeTool(DELEGATE, (("task", "查 C"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="收到一条"),
                lambda _frame: ModelDecision(content="又收到一条"),
                lambda _frame: ModelDecision(content="都收到了"),
            ),
            child_scripts=(
                lambda frame: ModelDecision(
                    command_requests=(
                        InvokeTool(
                            REPORT,
                            (
                                (
                                    "summary",
                                    f"结论来自 {frame.trigger_event.session_id}",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            interleave=True,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "同时查 A B C",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            parent_events = await runtime.snapshot(self.PARENT)
            delegated = frozenset(project_delegations(parent_events))
            self.assertEqual(len(delegated), 3)

            for index, frame in enumerate(model.parent_frames):
                visible = _visible_to(parent_events, frame)
                reclaimed = project_reclaimed(visible)
                if not reclaimed:
                    # 一条结论都还没回来，这一帧本来就不欠什么。
                    continue
                with self.subTest(decision=index):
                    # 已经有结论回来时，三个 delegate Command 都必须可见。
                    self.assertEqual(
                        frozenset(project_delegations(visible)),
                        delegated,
                        "有子已交回结论，但兄弟的 delegate Outcome 还没落库",
                    )
                    if reclaimed != delegated:
                        self.assertNotEqual(
                            _pending(visible),
                            frozenset(),
                            "父在还有子没交回结论时看到了空的待回收集合",
                        )
        finally:
            await scheduler.close()

    async def test_failing_child_is_reclaimed_as_a_failure_not_as_silence(self):
        def _boom(_frame):
            raise LLMProviderError("上游返回 500")

        delivered: list[tuple[str, str]] = []
        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(DELEGATE, (("task", "查清 A"),)),),
                ),
                lambda _frame: ModelDecision(content="子 Agent 没跑完，我换个做法"),
            ),
            child_scripts=(_boom,),
            delivered=delivered,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(self.PARENT, "查 A", delivery_id="user-1")

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            parent_events = await runtime.snapshot(self.PARENT)
            reports = _facts(parent_events, REPORT_FACT)

            self.assertEqual(len(reports), 1)
            report = reports[0]
            # 失败不被改写成「停机但无产出」，父能看出区别。
            self.assertIs(report.data["reported"], False)
            self.assertIsNone(report.data["summary"])
            self.assertIn("上游返回 500", report.data["failure"])
            # 失败也是终局：父不会拿着一个永不归零的待回收集合干等。
            self.assertEqual(
                project_reclaimed(parent_events),
                frozenset(project_delegations(parent_events)),
            )
            self.assertEqual(_pending(parent_events), frozenset())
            # 父被叫醒并做了下一步判断。
            self.assertEqual(len(model.parent_frames), 2)
            # 子那条裸错误没有冒到用户面前，用户该看到的是父的转述。
            self.assertEqual(delivered, [])
        finally:
            await scheduler.close()

    async def test_unexpected_child_failure_is_reclaimed_with_original_exception(
        self,
    ):
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(lambda _frame: ModelDecision(content="换路"),),
            child_scripts=(),
        )
        await runtime.create_session(self.PARENT)
        intent = DelegateIntent(
            "manual-command",
            self.PARENT,
            f"{self.PARENT}/sub-manual-command",
            "查 A",
        )
        child = intent.child_session_id
        await runtime.create_session(child)
        await runtime.receive_domain_fact(child, **task_fact_arguments(intent))
        try:
            from helperme.assistant.subagent.subagent import record_unexpected_return

            self.assertIsNone(
                await record_unexpected_return(
                    runtime._journal, child, LeaseLostError("stale")
                )
            )
            self.assertEqual(
                _facts(await runtime.snapshot(self.PARENT), REPORT_FACT), []
            )

            try:
                raise RuntimeError("disk vanished")
            except RuntimeError as error:
                parent, arguments = await record_unexpected_return(
                    runtime._journal, child, error
                )
                await host._transport("fact", parent, arguments)
            reports = _facts(await runtime.snapshot(self.PARENT), REPORT_FACT)
            self.assertEqual(len(reports), 1)
            self.assertIs(reports[0].data["reported"], False)
            self.assertIsNone(reports[0].data["summary"])
            self.assertIn("RuntimeError", reports[0].data["failure"])
            self.assertIn("disk vanished", reports[0].data["failure"])
            self.assertIn("Traceback", reports[0].data["failure"])
            self.assertEqual(
                project_reclaimed(await runtime.snapshot(self.PARENT)),
                frozenset({child}),
            )
        finally:
            await scheduler.close()

    async def test_parent_failure_still_reaches_the_user(self):
        def _boom(_frame):
            raise LLMProviderError("上游返回 500")

        delivered: list[tuple[str, str]] = []
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(_boom,),
            child_scripts=(),
            delivered=delivered,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(self.PARENT, "查 A", delivery_id="user-1")

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            # 拦的是子 Session 的对外输出，不是所有失败。
            self.assertEqual(len(delivered), 1)
            session_id, text = delivered[0]
            self.assertEqual(session_id, self.PARENT)
            self.assertIn("上游返回 500", text)
            # 父没有父，回收无处可去。
            self.assertEqual(
                _facts(await runtime.snapshot(self.PARENT), REPORT_FACT), []
            )
        finally:
            await scheduler.close()

    async def test_child_stopping_without_report_is_reclaimed_honestly(self):
        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(DELEGATE, (("task", "看一眼 B"),)),),
                ),
                lambda _frame: ModelDecision(content="子 Agent 没有产出"),
            ),
            child_scripts=(lambda _frame: ModelDecision(content="我看完了"),),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "看 B",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            reports = _facts(await runtime.snapshot(self.PARENT), REPORT_FACT)
            self.assertEqual(len(reports), 1)
            self.assertIs(reports[0].data["reported"], False)
            self.assertIsNone(reports[0].data["summary"])
        finally:
            await scheduler.close()

    async def test_subagent_output_is_not_routed_anywhere(self):
        delivered: list[tuple[str, str]] = []
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(DELEGATE, (("task", "查 C"),)),),
                ),
                lambda _frame: ModelDecision(content="好"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(REPORT, (("summary", "C 没有问题"),)),
                    ),
                ),
            ),
            delivered=delivered,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "查 C",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
            child_session_id = await self._child_session_id(runtime)

            routed = host.routed_sink(
                lambda session_id, _output_id, text: delivered.append((session_id, text))
            )
            await routed(child_session_id, "output-1", "子 Agent 的中间过程")
            await routed(self.PARENT, "output-2", "给用户的结论")

            self.assertEqual(delivered, [(self.PARENT, "给用户的结论")])
        finally:
            await scheduler.close()

    async def test_child_cannot_delegate_again(self):
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(DELEGATE, (("task", "查 D"),)),),
                ),
                lambda _frame: ModelDecision(content="好"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(REPORT, (("summary", "D 正常"),)),),
                ),
            ),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "查 D",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
            child_session_id = await self._child_session_id(runtime)

            self.assertNotIn(DELEGATE, CHILD_BUILTIN_TOOL_NAMES)
            self.assertEqual(
                [
                    schema["function"]["name"]
                    for schema in host.schemas(child_session_id)
                ],
                [REPORT],
            )
            self.assertNotIn(RECLAIM, CHILD_BUILTIN_TOOL_NAMES)
            refused = await host._delegate(
                _attempt_context(child_session_id),
                {"task": "再委派一层"},
            )
            self.assertIs(refused["ok"], False)
            self.assertEqual(refused["code"], "DELEGATION_NOT_ALLOWED")
            refused_reclaim = await host._reclaim_command(
                _attempt_context(child_session_id),
                {"child_session_id": child_session_id},
            )
            self.assertIs(refused_reclaim["ok"], False)
            self.assertEqual(refused_reclaim["code"], "RECLAIM_NOT_ALLOWED")
        finally:
            await scheduler.close()

    async def test_children_are_recognised_again_after_restart(self):
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(DELEGATE, (("task", "查 E"),)),),
                ),
                lambda _frame: ModelDecision(content="好"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(REPORT, (("summary", "E 正常"),)),),
                ),
            ),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "查 E",
            delivery_id="user-1",
        )
        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
        finally:
            await scheduler.close()
        child_session_id = await self._child_session_id(runtime)
        events = await runtime.snapshot(self.PARENT)

        restarted = SubAgentHost()
        restarted_scheduler = LocalSessionRouter(
            runtime,
            on_quiesced=restarted.on_quiesced,
        )
        restarted.attach(
            runtime, local_transport(runtime, restarted_scheduler, restarted)
        )
        try:
            self.assertFalse(restarted.is_subagent(child_session_id))
            pending = await restarted.rehydrate(self.PARENT)

            # 认回来是安全边界：不认回，子 Session 就没有能力限制了。
            self.assertTrue(restarted.is_subagent(child_session_id))
            self.assertEqual(
                restarted.tool_names(child_session_id),
                CHILD_BUILTIN_TOOL_NAMES,
            )
            # 已经回收过，不需要再推它。
            self.assertEqual(pending, ())
            self.assertEqual(project_delegations(events), (child_session_id,))
            self.assertEqual(
                project_reclaimed(events),
                frozenset({child_session_id}),
            )

            # 回收事实还没写下来时，同一个子就是待推进的那个。
            before_reclaim = tuple(
                event
                for event in events
                if not (
                    isinstance(event.payload, DomainFactCommitted)
                    and event.payload.fact_type == REPORT_FACT
                )
            )
            self.assertEqual(project_reclaimed(before_reclaim), frozenset())
            self.assertEqual(
                project_delegations(before_reclaim),
                (child_session_id,),
            )
        finally:
            await restarted_scheduler.close()

    async def test_child_recognises_itself_without_going_through_its_parent(self):
        """直接恢复一个子 Session 时，能力边界不能因为没经过父而丢失。"""

        host, _model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(DELEGATE, (("task", "查 F"),)),),
                ),
                lambda _frame: ModelDecision(content="好"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(REPORT, (("summary", "F 正常"),)),),
                ),
            ),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "查 F",
            delivery_id="user-1",
        )
        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
        finally:
            await scheduler.close()
        child_session_id = await self._child_session_id(runtime)

        restarted = SubAgentHost()
        restarted_scheduler = LocalSessionRouter(
            runtime,
            on_quiesced=restarted.on_quiesced,
        )
        restarted.attach(
            runtime, local_transport(runtime, restarted_scheduler, restarted)
        )
        try:
            pending = await restarted.rehydrate(child_session_id)

            self.assertEqual(pending, ())
            self.assertTrue(restarted.is_subagent(child_session_id))
            self.assertEqual(
                restarted.parent_of(child_session_id),
                self.PARENT,
            )
            self.assertEqual(
                project_parent(await runtime.snapshot(self.PARENT)),
                None,
            )
        finally:
            await restarted_scheduler.close()


    async def test_parent_reclaim_clears_pending_without_another_decision(self):
        hang = asyncio.Event()
        started = asyncio.Event()
        child_id: dict[str, str] = {}

        async def child_hang(_frame):
            started.set()
            await hang.wait()
            return ModelDecision(
                command_requests=(
                    InvokeTool(REPORT, (("summary", "不该被看见"),)),
                ),
            )

        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(DELEGATE, (("task", "查 A"),)),),
                ),
                lambda _frame: ModelDecision(
                    content="已停",
                    command_requests=(
                        InvokeTool(
                            RECLAIM,
                            (
                                ("child_session_id", child_id["id"]),
                                ("reason", "用户不要了"),
                            ),
                        ),
                    ),
                ),
            ),
            child_scripts=(child_hang,),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(self.PARENT, "查 A", delivery_id="user-1")

        try:
            await scheduler.wake(self.PARENT)
            await started.wait()
            child_id["id"] = project_delegations(
                await runtime.snapshot(self.PARENT)
            )[0]
            await runtime.receive_user_message(self.PARENT, "停", delivery_id="user-2")
            await scheduler.wake(self.PARENT)

            async with asyncio.timeout(5):
                while True:
                    reports = _facts(
                        await runtime.snapshot(self.PARENT),
                        REPORT_FACT,
                    )
                    if reports:
                        break
                    await asyncio.sleep(0)
            hang.set()
            await scheduler.join()

            parent_events = await runtime.snapshot(self.PARENT)
            reports = _facts(parent_events, REPORT_FACT)
            self.assertEqual(len(reports), 1)
            self.assertIs(reports[0].requests_decision, False)
            self.assertIs(reports[0].data["cancelled"], True)
            self.assertIs(reports[0].data["reported"], False)
            self.assertIsNone(reports[0].data["summary"])
            self.assertIsNone(reports[0].data["failure"])
            self.assertEqual(reports[0].data["reason"], "用户不要了")
            self.assertEqual(_pending(parent_events), frozenset())
            # 取消不单独要一次决策：delegate + 用户插话后的 reclaim。
            self.assertEqual(len(model.parent_frames), 2)
        finally:
            hang.set()
            await scheduler.close()

    async def test_parent_can_reclaim_then_delegate_again(self):
        hang = asyncio.Event()
        started = asyncio.Event()
        child_id: dict[str, str] = {}

        async def child_decide(frame):
            session_id = frame.trigger_event.session_id
            if child_id.get("id") in {None, session_id}:
                started.set()
                await hang.wait()
                return ModelDecision(
                    command_requests=(
                        InvokeTool(REPORT, (("summary", "旧子不该交回"),)),
                    ),
                )
            return ModelDecision(
                command_requests=(
                    InvokeTool(REPORT, (("summary", "按新参数查清了"),)),
                ),
            )

        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(DELEGATE, (("task", "按旧参数查"),)),),
                ),
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(
                            RECLAIM,
                            (("child_session_id", child_id["id"]),),
                        ),
                        InvokeTool(DELEGATE, (("task", "按新参数查"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="新结论到了"),
            ),
            child_scripts=(child_decide,),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(self.PARENT, "查 A", delivery_id="user-1")

        try:
            await scheduler.wake(self.PARENT)
            await started.wait()
            child_id["id"] = project_delegations(
                await runtime.snapshot(self.PARENT)
            )[0]
            await runtime.receive_user_message(
                self.PARENT,
                "参数不对，换一个",
                delivery_id="user-2",
            )
            await scheduler.wake(self.PARENT)
            async with asyncio.timeout(5):
                while True:
                    reports = _facts(
                        await runtime.snapshot(self.PARENT),
                        REPORT_FACT,
                    )
                    if any(report.data.get("cancelled") is True for report in reports):
                        break
                    await asyncio.sleep(0)
            hang.set()
            await scheduler.join()

            parent_events = await runtime.snapshot(self.PARENT)
            children = project_delegations(parent_events)
            self.assertEqual(len(children), 2)
            reports = _facts(parent_events, REPORT_FACT)
            by_child = {report.data["child_session_id"]: report for report in reports}
            self.assertIs(by_child[children[0]].data["cancelled"], True)
            self.assertIs(by_child[children[1]].data["reported"], True)
            self.assertEqual(
                by_child[children[1]].data["summary"],
                "按新参数查清了",
            )
            self.assertEqual(_pending(parent_events), frozenset())
            self.assertEqual(len(model.parent_frames), 3)
        finally:
            hang.set()
            await scheduler.close()

    async def test_reclaim_of_finished_child_is_idempotent(self):
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(InvokeTool(DELEGATE, (("task", "查 A"),)),),
                ),
                lambda _frame: ModelDecision(content="收到"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(REPORT, (("summary", "A 没问题"),)),
                    ),
                ),
            ),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(self.PARENT, "查 A", delivery_id="user-1")
        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
            child_session_id = await self._child_session_id(runtime)
            already = await host._reclaim_command(
                _attempt_context(self.PARENT),
                {"child_session_id": child_session_id},
            )
            unknown = await host._reclaim_command(
                _attempt_context(self.PARENT),
                {"child_session_id": "not-a-child"},
            )
            self.assertEqual(already["code"], "ALREADY_RECLAIMED")
            self.assertEqual(unknown["code"], "UNKNOWN_CHILD")
            self.assertEqual(
                len(_facts(await runtime.snapshot(self.PARENT), REPORT_FACT)),
                1,
            )
        finally:
            await scheduler.close()


class SubAgentPendingInstructionTest(unittest.IsolatedAsyncioTestCase):
    """父在结论不齐时该被约束，齐了就不该再被约束。"""

    PARENT = "parent-session"

    @staticmethod
    def _decision_maker(runtime, llm, host):
        # runtime 自己就有 snapshot(session_id)，直接当 journal 用。
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
        )

    def _frame(self, position: int):
        return SimpleNamespace(
            state=SimpleNamespace(
                session_id=self.PARENT,
                visible_event_ids=(),
                steps=(),
            ),
            trigger_event=SimpleNamespace(event_id="trigger-1"),
            decision_cursor=1,
            basis_state_version="basis-1",
            observed_journal_position=position,
        )

    async def _delegated_parent(self):
        """跑完一轮委派，交出父 Journal 与宿主。"""

        host = SubAgentHost()
        runtime = AgentRuntime(
            MemoryJournal(),
            _ParentChildDecisions(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(InvokeTool(DELEGATE, (("task", "查 A"),)),),
                    ),
                    lambda _frame: ModelDecision(content="收到"),
                ),
                (
                    lambda _frame: ModelDecision(
                        command_requests=(
                            InvokeTool(REPORT, (("summary", "A 没问题"),)),
                        ),
                    ),
                ),
            ),
            dict(host.bindings()),
            SequentialIds(),
        )
        scheduler = LocalSessionRouter(runtime, on_quiesced=host.on_quiesced)
        host.attach(runtime, local_transport(runtime, scheduler, host))
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "查 A",
            delivery_id="user-1",
        )
        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
        finally:
            await scheduler.close()
        return host, runtime, await runtime.snapshot(self.PARENT)

    async def test_pending_child_does_not_change_system_prompt(self):
        """同一份 Journal，两个冻结位置，得到两份不同的提示。

        位置取自 frame 而不是「现在」：结论已经回来了，但重放一次早先的决策，
        那次决策看到的世界里子还没回来，提示必须照旧带上约束。
        """

        host, runtime, parent_events = await self._delegated_parent()
        first_report = next(
            event
            for event in parent_events
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == REPORT_FACT
        )
        before = first_report.sequence - 1
        after = parent_events[-1].sequence

        visible_before = _visible_to(parent_events, self._frame(before))
        self.assertNotEqual(project_pending(visible_before), frozenset())
        self.assertEqual(project_pending(parent_events), frozenset())

        llm = _RecordingLlm()
        maker = self._decision_maker(runtime, llm, host)
        await maker.decide(self._frame(before))
        await maker.decide(self._frame(after))

        self.assertEqual(len(llm.system_prompts), 2)
        self.assertEqual(llm.system_prompts[0], llm.system_prompts[1])

    async def test_report_fact_does_not_carry_a_pending_count(self):
        """「还差谁」不冻进事实。

        冻进去就要求两个并行的子在父维度串行读写，才能各自算对自己那一条。
        """

        _host, _runtime, parent_events = await self._delegated_parent()
        reports = _facts(parent_events, REPORT_FACT)

        self.assertEqual(len(reports), 1)
        self.assertEqual(
            set(reports[0].data),
            {
                "child_session_id",
                "reported",
                "summary",
                "failure",
                "cancelled",
                "reason",
            },
        )
        self.assertIs(reports[0].data["cancelled"], False)
        self.assertIsNone(reports[0].data["reason"])


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
            **report_arguments(
                intent,
                return_data(intent, reported=True, summary="查清了", failure=None),
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
    async def test_external_schemas_bypass_builtin_name_filter(self):
        from helperme.assistant.toolsets import ToolSurface
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
        maker = SubAgentPendingInstructionTest._decision_maker(runtime, None, host)
        maker._surface = surface
        schemas, _ = maker.schemas_for(SimpleNamespace(session_id="child"), ())
        names = {schema["function"]["name"] for schema in schemas}
        self.assertIn("demo_ping", names)
        self.assertIn("load_toolset", names)
        self.assertIn("write_file", names)
        self.assertNotIn("execute_command", names)

    def test_child_builtin_names_exclude_execution_and_recursion(self):
        for name in (
            "execute_command",
            DELEGATE,
            RECLAIM,
        ):
            with self.subTest(tool=name):
                self.assertNotIn(name, CHILD_BUILTIN_TOOL_NAMES)

    def test_unknown_session_has_no_policy(self):
        host = SubAgentHost()
        self.assertIsNone(host.tool_names("plain-session"))
        self.assertIsNone(host.system_prompt("plain-session"))
        self.assertEqual(
            [schema["function"]["name"] for schema in host.schemas("plain")],
            [DELEGATE, RECLAIM],
        )

    def test_session_without_any_report_projects_to_none(self):
        self.assertIsNone(project_report(()))

    def test_nothing_delegated_means_nothing_pending(self):
        self.assertEqual(project_pending(()), frozenset())
