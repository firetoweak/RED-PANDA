"""SubAgent 委派与回收。

子 Session 是普通独立 Session：同一套 create / advance / recover，自己的
Journal 与判定。父子关系只存在于 Assistant 侧，用因果事实表达，Runtime 不
增加 `parent_session_id` 或 `agent_type`。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from redpanda.assistant.context.prompt import SUBAGENT_PROMPT
from redpanda.assistant.delivery import DeliverySink, emit_delivery
from redpanda.assistant.host.ipc import ProcessFailure
from redpanda.runtime import (
    AgentRuntime,
    CommandOutcome,
    CommandOutcomeReceived,
    CommandPhase,
    DeliveryConflictError,
    DomainFactCommitted,
    Event,
    InvokeTool,
    LeaseLostError,
    OutcomeStatus,
    StepCommitted,
    StateProjector,
    ToolBinding,
)
from redpanda.runtime.dispatcher import AttemptContext
from redpanda.runtime.events import DeliveryIdentity, EventDraft
from redpanda.runtime.journal.api import Journal
from redpanda.runtime.model import CanonicalState


DELEGATE = "delegate"
RECLAIM = "reclaim"
REPORT = "report"

TASK_FACT = "subagent.task"
REPORT_FACT = "subagent.report"
RETURN_FACT = "subagent.return"

FACT_SOURCE = "subagent"


@dataclass(frozen=True, slots=True)
class DelegateIntent:
    command_id: str
    parent_session_id: str
    child_session_id: str
    task: str
    resolve_conflicts_of: str | None = None


@dataclass(frozen=True, slots=True)
class SubAgentTask:
    child_session_id: str
    parent_session_id: str
    delegate_command_id: str
    task: str
    resolve_conflicts_of: str | None = None


def child_session_id(parent_session_id: str, command_id: str) -> str:
    if type(parent_session_id) is not str or not parent_session_id:
        raise ValueError("parent Session id must be a non-empty str")
    if type(command_id) is not str or not command_id:
        raise ValueError("delegate command id must be a non-empty str")
    return f"{parent_session_id}/sub-{command_id}"


def task_fact_arguments(intent: DelegateIntent) -> dict[str, object]:
    return {
        "fact_type": TASK_FACT,
        "data": {
            "task": intent.task,
            "parent_session_id": intent.parent_session_id,
            "delegate_command_id": intent.command_id,
            "resolve_conflicts_of": intent.resolve_conflicts_of,
        },
        "delivery_id": f"{intent.command_id}:task",
        "source": FACT_SOURCE,
        "requests_decision": True,
    }


def task_from_arguments(
    child_id: str,
    arguments: Mapping[str, object],
) -> SubAgentTask:
    if set(arguments) != {
        "fact_type",
        "data",
        "delivery_id",
        "source",
        "requests_decision",
    }:
        raise ValueError("subagent task fact envelope 字段不匹配")
    if arguments["fact_type"] != TASK_FACT:
        raise ValueError("subagent task fact_type 无效")
    if arguments["source"] != FACT_SOURCE:
        raise ValueError("subagent task source 无效")
    if arguments["requests_decision"] is not True:
        raise ValueError("subagent task 必须请求首次决策")
    data = arguments["data"]
    if not isinstance(data, Mapping) or set(data) != {
        "task",
        "parent_session_id",
        "delegate_command_id",
        "resolve_conflicts_of",
    }:
        raise ValueError("subagent 任务事实 data 字段不匹配")
    task = data["task"]
    parent_session_id = data["parent_session_id"]
    command_id = data["delegate_command_id"]
    if type(task) is not str or not task:
        raise ValueError("subagent 任务事实 task 无效")
    if type(parent_session_id) is not str or not parent_session_id:
        raise ValueError("subagent 任务事实 parent_session_id 无效")
    if type(command_id) is not str or not command_id:
        raise ValueError("subagent 任务事实 delegate_command_id 无效")
    if child_id != child_session_id(parent_session_id, command_id):
        raise ValueError("subagent task 的 child identity 与 delegate Command 不一致")
    if arguments["delivery_id"] != f"{command_id}:task":
        raise ValueError("subagent task delivery identity 无效")
    resolve = data["resolve_conflicts_of"]
    if resolve is not None and (type(resolve) is not str or not resolve):
        raise ValueError("resolve_conflicts_of 必须是非空 string|null")
    return SubAgentTask(child_id, parent_session_id, command_id, task, resolve)


def return_data(
    child_session_id: str,
    *,
    reported: bool,
    summary: str | None,
    failure: str | None,
    cancelled: bool = False,
    reason: str | None = None,
) -> dict[str, object]:
    return {
        "child_session_id": child_session_id,
        "reported": reported,
        "summary": summary,
        "failure": failure,
        "cancelled": cancelled,
        "reason": reason,
    }


def report_arguments(
    session_id: str,
    data: Mapping[str, object],
) -> dict[str, object]:
    """Parent report payload. Parent-initiated cancel does not request a Step."""

    return dict(
        fact_type=REPORT_FACT,
        data=dict(data),
        delivery_id=f"{session_id}:report",
        source=FACT_SOURCE,
        requests_decision=data.get("cancelled") is not True,
    )


def _return_event(events: Sequence[Event]) -> Event | None:
    return next(
        (
            event
            for event in events
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == RETURN_FACT
        ),
        None,
    )


def project_returned(events: Sequence[Event]) -> bool:
    return _return_event(events) is not None


def _delegated_result(child_id: str) -> dict[str, object]:
    return {"ok": True, "code": "DELEGATED", "data": {"child_session_id": child_id}}


async def persist_return(
    journal: Journal,
    session_id: str,
    data: Mapping[str, object],
) -> Event:
    """Write subagent.return once. The first durable fact wins."""

    existing = _return_event(await journal.snapshot(session_id))
    if existing is not None:
        return existing
    try:
        result = await journal.accept_delivery(
            EventDraft(
                event_id=f"event_{uuid4().hex}",
                session_id=session_id,
                payload=DomainFactCommitted(RETURN_FACT, dict(data)),
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity(FACT_SOURCE, f"{session_id}:return"),
            )
        )
    except DeliveryConflictError:
        existing = _return_event(await journal.snapshot(session_id))
        if existing is None:
            raise
        return existing
    return result.event


async def record_unexpected_return(journal: Journal, session_id: str, error: Exception):
    """Persist the failure without needing an assembled Assistant or an IPC reader."""
    if isinstance(error, LeaseLostError):
        return None
    parent = project_parent(await journal.snapshot(session_id))
    if parent is None:
        return None
    returned = await persist_return(
        journal,
        session_id,
        return_data(
            session_id,
            reported=False,
            summary=None,
            failure=ProcessFailure.capture(error).render(),
        ),
    )
    return parent, report_arguments(session_id, returned.payload.data)


async def record_interrupted_return(journal: Journal, session_id: str) -> None:
    """新 Worker 独占接管后，把未完成调用作为中断事实交回父会话。"""
    events = await journal.snapshot(session_id)
    if project_parent(events) is None or _return_event(events) is not None:
        return
    state = StateProjector().project(session_id, events).state
    unfinished = [
        command.command.command_id
        for command in state.commands
        if command.phase is CommandPhase.UNKNOWN
    ]
    if not unfinished:
        return
    await persist_return(
        journal,
        session_id,
        return_data(
            session_id,
            reported=False,
            summary=None,
            failure=(
                "子 Agent 执行被中断：以下工具调用已经开始但没有返回结果，"
                "不知道是否生效，也没有自动重试。需要的话重新 delegate。"
                " 子 Agent 内部的调用 id（仅供排查）: " + ", ".join(unfinished)
            ),
        ),
    )


SubAgentActivitySink = Callable[[str, bool], None]


CHILD_BUILTIN_TOOL_NAMES = frozenset(
    {
        "glob",
        "grep",
        "read_file",
        "read_artifact",
        "load_skill",
        "read_skill_resource",
        "write_file",
        "apply_patch",
        "replace_all",
        "restore_workspace",
        "update_plan",
        REPORT,
    }
)
"""子 Session 能看见的内建工具；外部工具从只读 Server 目录渐进加载。

显式列举：新增任何工具默认进不来，要进必须有人明确加。
`execute_command` 不在其中——执行等进程沙箱。`get_changes` 不在其中——子工作树
没有用户 Git 仓库，比较由父按会话拉取。`DELEGATE` 也不在其中，递归委派因此被
同一份名单挡住。
"""


DELEGATE_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": DELEGATE,
        "description": (
            "把一件可独立完成的任务委派给子 Agent。"
            "子 Agent 有自己的上下文，过程不会占用当前对话；"
            "它可在自己的文件副本里修改文件，不能执行命令。"
            "成果交回后用 compare_subagent 验收，merge_subagent 经授权合入用户工作区。"
            "本次调用只返回“已创建”，结论稍后作为一条事实送回。"
            "多件互不依赖的任务可以在同一次决策里各发一次 delegate，"
            "子 Agent 之间并行推进；并行的子 Agent 改同一批文件，合入时会冲突。"
            "结论一条条回来，不是一次性交齐。"
            "不再需要某个还在工作的子 Agent 时，用 reclaim 收回它，不要空等。"
            "回来的也可能是失败：failure 非空表示该子 Agent 没能跑完，"
            "字段里是失败原因，由你判断重派、换做法还是如实告诉用户。"
            "cancelled=true 表示你已经收回，不是子 Agent 失败。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "交给子 Agent 的完整任务描述。它看不到当前对话，"
                        "所需背景必须写在这里。写明只调查还是要修改，以及允许改动的范围。"
                        "路径先自己核实再写进来；写明找不到目标时如实报告，不要换目标。"
                    ),
                },
                "resolve_conflicts_of": {
                    "type": ["string", "null"],
                    "description": "可选：合入冲突的原 delegate 调用 id，新子从带冲突标记的工作树开工。",
                },
            },
            "required": ["task"],
        },
    },
}


RECLAIM_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": RECLAIM,
        "description": (
            "收回一个还在工作的子 Agent。"
            "用户不要继续、任务已变、或这个子 Agent 的参数已经不对时使用。"
            "收回后它会停下来，不再有结论送回；需要的话可以再 delegate 一个新的。"
            "已经交回结论的子 Agent 再收回没有效果。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "child_session_id": {
                    "type": "string",
                    "description": (
                        "要收回的子 Agent，来自先前 delegate 返回的 child_session_id。"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "为何收回，供后续判断阅读。",
                },
            },
            "required": ["child_session_id"],
        },
    },
}


REPORT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": REPORT,
        "description": (
            "把结论交回父 Agent。这是唯一会被父 Agent 看到的通道。"
            "summary 要能独立读懂：结论、依据、以及没能确定的部分。"
            "改过文件时说明改了哪些、为什么改，不贴 diff。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "面向父 Agent 的完整结论，含依据、改动的文件与原因、遗留问题。",
                },
            },
            "required": ["summary"],
        },
    },
}


def _frozen_delegate_child(command_id: str, value: object) -> tuple[str, str] | None:
    """成功 DELEGATED Outcome 里冻结的 (parent, child)。

    子 id 必须仍是 `{parent}/sub-{command_id}`。父身份可以不是正在读
    这条线的 Session：分支重放会把事件收成当前身份，Outcome 不会跟着改。
    """

    if not isinstance(value, Mapping) or value.get("ok") is not True:
        return None
    if set(value) != {"ok", "code", "data"} or value.get("code") != "DELEGATED":
        return None
    data = value.get("data")
    if not isinstance(data, Mapping) or set(data) != {"child_session_id"}:
        return None
    child_id = data.get("child_session_id")
    suffix = f"/sub-{command_id}"
    if type(child_id) is not str or not child_id.endswith(suffix):
        return None
    parent = child_id[: -len(suffix)]
    if not parent:
        return None
    return parent, child_id


def _frozen_delegate_children(events: Sequence[Event]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for event in events:
        payload = event.payload
        if not isinstance(payload, CommandOutcomeReceived):
            continue
        if payload.outcome.status is not OutcomeStatus.SUCCEEDED:
            continue
        child = _frozen_delegate_child(payload.command_id, payload.outcome.value)
        if child is not None:
            found[payload.command_id] = child
    return found


def project_delegate_intents(
    events: Sequence[Event],
) -> tuple[DelegateIntent, ...]:
    """从父 Step 已提交的 delegate Command 冻结全部 child identity。

    已有成功 Outcome 时用里面那份 child id。还没有 Outcome 的命令只会出现
    在提交它的会话上，这时才用事件上的 session id 派生。
    """

    frozen = _frozen_delegate_children(events)
    intents: list[DelegateIntent] = []
    for event in events:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            continue
        for command in payload.step.commands:
            effect = command.effect
            if isinstance(effect, InvokeTool) and effect.name == DELEGATE:
                arguments = effect.argument_dict()
                task = arguments.get("task")
                if type(task) is not str or not task.strip():
                    continue
                parent_session_id = event.session_id
                child_id = child_session_id(parent_session_id, command.command_id)
                committed = frozen.get(command.command_id)
                if committed is not None:
                    parent_session_id, child_id = committed
                intents.append(
                    DelegateIntent(
                        command.command_id,
                        parent_session_id,
                        child_id,
                        task.strip(),
                        arguments.get("resolve_conflicts_of"),
                    )
                )
    return tuple(intents)


def project_delegations(events: Sequence[Event]) -> tuple[str, ...]:
    """父 Session 已提交的有效 delegate Command 所确定的 child ids。"""

    return tuple(intent.child_session_id for intent in project_delegate_intents(events))


def project_failed_delegations(events: Sequence[Event]) -> frozenset[str]:
    """已经得到确定失败 Outcome 的 delegate；unknown 不在这里。"""

    by_command = {
        intent.command_id: intent for intent in project_delegate_intents(events)
    }
    failed: set[str] = set()

    for event in events:
        payload = event.payload
        if not isinstance(payload, CommandOutcomeReceived):
            continue
        intent = by_command.get(payload.command_id)
        if intent is None:
            continue
        if payload.outcome.status is not OutcomeStatus.SUCCEEDED:
            failed.add(intent.child_session_id)
            continue
        value = payload.outcome.value
        if not isinstance(value, Mapping) or type(value.get("ok")) is not bool:
            raise ValueError("delegate outcome 必须是带 ok 的 object")
        if value["ok"] is False:
            if (
                set(value) != {"ok", "code", "data", "error"}
                or type(value["code"]) is not str
                or not value["code"]
                or not isinstance(value["data"], Mapping)
                or type(value["error"]) is not str
                or not value["error"]
            ):
                raise ValueError("delegate failure outcome 字段不匹配")
            failed.add(intent.child_session_id)
            continue
        if set(value) != {"ok", "code", "data"} or value["code"] != "DELEGATED":
            raise ValueError("delegate success outcome 字段不匹配")
        data = value.get("data")
        if not isinstance(data, Mapping) or set(data) != {"child_session_id"}:
            raise ValueError("delegate outcome data 无效")
        if data["child_session_id"] != intent.child_session_id:
            raise ValueError("delegate outcome child_session_id 与 Command 不一致")
    return frozenset(failed)


def project_task(events: Sequence[Event]) -> SubAgentTask | None:
    """从子 Journal 读取并严格校验唯一的委派身份。"""

    tasks = [
        event
        for event in events
        if isinstance(event.payload, DomainFactCommitted)
        and event.payload.fact_type == TASK_FACT
    ]
    if not tasks:
        return None
    if len(tasks) != 1:
        raise ValueError("subagent Journal 必须只有一条 task 事实")
    event = tasks[0]
    payload = event.payload
    if payload.requests_decision is not True:
        raise ValueError("subagent task 必须请求首次决策")
    data = payload.data
    if not isinstance(data, Mapping) or set(data) != {
        "task",
        "parent_session_id",
        "delegate_command_id",
        "resolve_conflicts_of",
    }:
        raise ValueError("subagent 任务事实 data 字段不匹配")
    task = data["task"]
    parent_session_id = data["parent_session_id"]
    command_id = data["delegate_command_id"]
    if type(task) is not str or not task:
        raise ValueError("subagent 任务事实 task 无效")
    if type(parent_session_id) is not str or not parent_session_id:
        raise ValueError("subagent 任务事实 parent_session_id 无效")
    if type(command_id) is not str or not command_id:
        raise ValueError("subagent 任务事实 delegate_command_id 无效")
    expected_child = child_session_id(parent_session_id, command_id)
    if event.session_id != expected_child:
        raise ValueError("subagent task 的 child identity 与 delegate Command 不一致")
    delivery = event.delivery
    if (
        delivery is None
        or delivery.source != FACT_SOURCE
        or delivery.delivery_id != f"{command_id}:task"
    ):
        raise ValueError("subagent task delivery identity 无效")
    resolve = data["resolve_conflicts_of"]
    if resolve is not None and (type(resolve) is not str or not resolve):
        raise ValueError("resolve_conflicts_of 必须是非空 string|null")
    return SubAgentTask(
        event.session_id,
        parent_session_id,
        command_id,
        task,
        resolve,
    )


def project_parent(events: Sequence[Event]) -> str | None:
    """从一条 Session 自己的 Journal 判断它是不是子 Session。"""

    task = project_task(events)
    return None if task is None else task.parent_session_id


def project_pending(events: Sequence[Event]) -> frozenset[str]:
    """父已委派但还没交回结论的子 Session。"""

    return (
        frozenset(project_delegations(events))
        - project_failed_delegations(events)
        - project_reclaimed(events)
    )


@dataclass(frozen=True, slots=True)
class UnreportedChildReturn:
    """本线 Journal 还没有回收事实，但子会话自己已经写下终局。"""

    command_id: str
    child_session_id: str
    reported: bool
    summary: str | None
    failure: str | None
    cancelled: bool
    reason: str | None


def project_unreported_returns(
    events: Sequence[Event],
    returns_by_child: Mapping[str, Mapping[str, object]],
) -> tuple[UnreportedChildReturn, ...]:
    """待回收集合里，子 Journal 已经有回传的那些。

    回收事实投到提交委派的那条会话。分支只继承截止点之前的前缀，
    读子会话的回传才能知道它已经结束，不必把后来的回收复制进这条线。
    """

    intents = {
        intent.child_session_id: intent for intent in project_delegate_intents(events)
    }
    found: list[UnreportedChildReturn] = []
    for child_id in project_pending(events):
        data = returns_by_child.get(child_id)
        if data is None:
            continue
        intent = intents[child_id]
        found.append(
            UnreportedChildReturn(
                intent.command_id,
                child_id,
                data["reported"],
                data["summary"],
                data["failure"],
                data["cancelled"],
                data["reason"],
            )
        )
    return tuple(sorted(found, key=lambda item: item.command_id))


def project_reclaimed(events: Sequence[Event]) -> frozenset[str]:
    """父 Session 已经收到过结论的那些子 Session。"""

    reclaimed: set[str] = set()
    for event in events:
        payload = event.payload
        if (
            not isinstance(payload, DomainFactCommitted)
            or payload.fact_type != REPORT_FACT
        ):
            continue
        data = payload.data
        if not isinstance(data, Mapping):
            raise ValueError("subagent 回收事实 data 无效")
        reclaimed.add(data["child_session_id"])
    return frozenset(reclaimed)


def project_report(events: Sequence[Event]) -> str | None:
    """从子 Session 的 Journal 取回最后一次成功的 report 内容。"""

    report_commands: set[str] = set()
    for event in events:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            continue
        for command in payload.step.commands:
            effect = command.effect
            if isinstance(effect, InvokeTool) and effect.name == REPORT:
                report_commands.add(command.command_id)

    summary: str | None = None
    for event in events:
        payload = event.payload
        if (
            not isinstance(payload, CommandOutcomeReceived)
            or payload.command_id not in report_commands
            or payload.outcome.status is not OutcomeStatus.SUCCEEDED
        ):
            continue
        value = payload.outcome.value
        if not isinstance(value, Mapping):
            raise ValueError("report outcome 必须是 object")
        data = value.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("report outcome data 无效")
        reported = data.get("summary")
        if type(reported) is not str or not reported:
            raise ValueError("report outcome summary 无效")
        summary = reported
    return summary


class SubAgentHost:
    """委派、回收，以及子 Session 的策略边界。"""

    def __init__(
        self,
        activity_sink: SubAgentActivitySink | None = None,
    ) -> None:
        self._runtime: AgentRuntime | None = None
        self._transport = None
        self._parents: dict[str, str] = {}
        self._returned: set[str] = set()
        # 仅供显示刷新；执行判断始终从 Journal 投影。
        self._visible_pending: dict[str, set[str]] = {}
        self._activity_sink = activity_sink

    def attach(self, runtime: AgentRuntime, transport) -> None:
        self._runtime = runtime
        self._transport = transport

    def is_subagent(self, session_id: str) -> bool:
        return session_id in self._parents

    def has_returned(self, session_id: str) -> bool:
        return session_id in self._returned

    def parent_of(self, session_id: str) -> str | None:
        return self._parents.get(session_id)

    def _publish_activity(self, parent_session_id: str) -> None:
        """异步刷新显示；不进入委派与回收的执行闭环。"""

        if self._activity_sink is not None:
            asyncio.get_running_loop().call_soon(self._emit_activity, parent_session_id)

    def _emit_activity(self, parent_session_id: str) -> None:
        assert self._activity_sink is not None
        self._activity_sink(
            parent_session_id,
            bool(self._visible_pending.get(parent_session_id)),
        )

    async def refresh_activity(self, session_id: str) -> None:
        events = await self._require_runtime().snapshot(session_id)
        if project_parent(events) is None:
            self._visible_pending[session_id] = set(project_pending(events))
            self._publish_activity(session_id)

    def tool_names(self, session_id: str) -> frozenset[str] | None:
        """本 Session 允许出现的工具名；None 表示不设限。"""

        return CHILD_BUILTIN_TOOL_NAMES if self.is_subagent(session_id) else None

    def system_prompt(self, session_id: str) -> str | None:
        return SUBAGENT_PROMPT if self.is_subagent(session_id) else None

    def schemas(self, session_id: str) -> list[dict[str, object]]:
        if self.is_subagent(session_id):
            return [REPORT_SCHEMA]
        return [DELEGATE_SCHEMA, RECLAIM_SCHEMA]

    def bindings(self) -> dict[str, ToolBinding]:
        return {
            DELEGATE: ToolBinding(self._delegate, decision_on_outcome=False),
            RECLAIM: ToolBinding(self._reclaim_command, decision_on_outcome=False),
            REPORT: ToolBinding(self._report, decision_on_outcome=False),
        }

    async def note_returned(self, session_id: str) -> bool:
        """A child with subagent.return has no more work. Do not wake it."""

        if session_id in self._returned:
            return True
        if _return_event(await self._require_runtime().snapshot(session_id)) is None:
            return False
        self._returned.add(session_id)
        return True

    async def cancel(self, session_id: str, arguments: Mapping[str, object]) -> None:
        """Persist a parent-initiated return and deliver the report. Tests use this."""

        parent_session_id = arguments["parent_session_id"]
        if type(parent_session_id) is not str or not parent_session_id:
            raise ValueError("reclaim parent_session_id 无效")
        reason = arguments.get("reason")
        if reason is not None and type(reason) is not str:
            raise TypeError("reclaim reason 必须是 string|null")
        await self._reclaim(
            session_id,
            parent_session_id,
            summary=None,
            failure=None,
            cancelled=True,
            reason=reason.strip() if type(reason) is str and reason.strip() else None,
        )

    def routed_sink(self, sink: DeliverySink, observation_sink: DeliverySink | None = None) -> DeliverySink:
        """子正文只路由到观察端口，父模型仍只收到 report。"""

        async def routed(session_id: str, output_id: str, text: str) -> None:
            if self.is_subagent(session_id):
                if observation_sink is not None:
                    await emit_delivery(observation_sink, session_id, output_id, text)
                return
            await emit_delivery(sink, session_id, output_id, text)

        return routed

    async def rehydrate(self, session_id: str) -> tuple[str, ...]:
        """重启后认回父子关系，并唤醒还没回收的子 Session。

        `_parents` 是进程内的可丢弃缓存。不重建它，重启后的子 Session 会
        失去能力边界，未回收的委派也再没有人推进。恢复的可能是父，也可能
        直接就是某个子，两个方向都要认得出来。
        """

        runtime = self._require_runtime()
        events = await runtime.snapshot(session_id)
        parent_session_id = project_parent(events)
        if parent_session_id is not None:
            self._parents[session_id] = parent_session_id
            for event in events:
                if (
                    isinstance(event.payload, DomainFactCommitted)
                    and event.payload.fact_type == RETURN_FACT
                ):
                    self._returned.add(session_id)
                    await self._transport(
                        "fact",
                        parent_session_id,
                        report_arguments(session_id, event.payload.data),
                    )
            return ()
        pending_ids = project_pending(events)
        intents = project_delegate_intents(events)
        state = await runtime.state(session_id)
        commands = {
            command.command.command_id: command for command in state.commands
        }
        pending: list[str] = []
        for intent in intents:
            self._parents[intent.child_session_id] = session_id
            command = commands[intent.command_id]
            if command.phase is CommandPhase.UNKNOWN:
                await self._transport(
                    "create_child",
                    intent.child_session_id,
                    task_fact_arguments(intent),
                )
                attempt = command.attempts[-1]
                await runtime.dispatcher.accept_outcome(
                    session_id,
                    intent.command_id,
                    attempt.attempt_id,
                    attempt.started_event_id,
                    CommandOutcome(
                        OutcomeStatus.SUCCEEDED,
                        value=_delegated_result(intent.child_session_id),
                    ),
                )
            if intent.child_session_id not in pending_ids:
                continue
            pending.append(intent.child_session_id)
            if command.phase is CommandPhase.TERMINAL:
                await self._transport("resume", intent.child_session_id, {})
        self._visible_pending[session_id] = set(pending)
        self._publish_activity(session_id)
        return tuple(pending)

    async def on_quiesced(
        self,
        session_id: str,
        state: CanonicalState,
    ) -> None:
        """Session 静止时判断是否到了回收一个子 Session 的时候。"""

        parent_session_id = self._parents.get(session_id)
        if parent_session_id is None:
            return
        # 子没有人，落到等外部事实就等于本轮做完了。等授权或等命令都不是。
        # 允许递归委派后，这里还要加上「没有未回收的子 Session」。
        if state.waiting_for != ("external_fact",):
            return
        runtime = self._require_runtime()
        summary = project_report(await runtime.snapshot(session_id))
        await self._reclaim(
            session_id,
            parent_session_id,
            summary=summary,
            failure=None,
        )

    async def on_failed(self, session_id: str, message: str) -> None:
        """子 Session 撞上已识别的失败，也是一种终局。

        失败不会让它静止，不回收父就一直等，待回收集合永远清不空。
        失败原文原样交给父：父是 Judge，重试还是换路由它判断，这里不改写、
        不降级成「无产出」。
        """

        await self._reclaim_failure(session_id, message)

    async def _reclaim_failure(self, session_id: str, message: str) -> None:
        parent_session_id = await self._resolve_parent(session_id)
        if parent_session_id is None:
            return
        await self._reclaim(
            session_id,
            parent_session_id,
            summary=None,
            failure=message,
        )

    async def _resolve_parent(self, session_id: str) -> str | None:
        parent_session_id = self._parents.get(session_id)
        if parent_session_id is not None:
            return parent_session_id
        parent_session_id = project_parent(
            await self._require_runtime().snapshot(session_id)
        )
        if parent_session_id is not None:
            self._parents[session_id] = parent_session_id
        return parent_session_id

    async def _reclaim(
        self,
        session_id: str,
        parent_session_id: str,
        *,
        summary: str | None,
        failure: str | None,
        cancelled: bool = False,
        reason: str | None = None,
    ) -> None:
        """把一个子 Session 的终局交回父。一个子最多回收一次。

        回收事实只记这个子自己的终局。「还差谁」是派生值，由父在决策时从自己
        已冻结的事实里投影；冻进事实就要求两个并行的子在父维度串行读写。
        已经有终局时沿用先写入的那条，包括父取消与子自己交回的竞态。
        """

        returned = await persist_return(
            self._require_runtime()._journal,
            session_id,
            return_data(
                session_id,
                reported=summary is not None,
                summary=summary,
                failure=failure,
                cancelled=cancelled,
                reason=reason,
            ),
        )
        await self._transport(
            "fact",
            parent_session_id,
            report_arguments(session_id, returned.payload.data),
        )
        self._returned.add(session_id)

    async def _reclaim_command(
        self,
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        if self.is_subagent(context.session_id):
            return {
                "ok": False,
                "code": "RECLAIM_NOT_ALLOWED",
                "data": {"session_id": context.session_id},
                "error": "子 Agent 不能收回其他子 Agent",
            }
        child_session_id = arguments.get("child_session_id")
        if type(child_session_id) is not str or not child_session_id.strip():
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "data": {"child_session_id": child_session_id},
                "error": "child_session_id 必须是非空字符串",
            }
        child_session_id = child_session_id.strip()
        reason = arguments.get("reason")
        if reason is not None and type(reason) is not str:
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "data": {"reason": reason},
                "error": "reason 必须是字符串",
            }
        reason_value = reason.strip() if type(reason) is str and reason.strip() else None
        events = await self._require_runtime().snapshot(context.session_id)
        if (
            child_session_id not in project_delegations(events)
            or child_session_id in project_failed_delegations(events)
        ):
            return {
                "ok": False,
                "code": "UNKNOWN_CHILD",
                "data": {"child_session_id": child_session_id},
                "error": "不是当前会话委派的子 Agent",
            }
        if child_session_id in project_reclaimed(events):
            return {
                "ok": True,
                "code": "ALREADY_RECLAIMED",
                "data": {"child_session_id": child_session_id},
            }
        await self._transport(
            "reclaim_child",
            child_session_id,
            {
                "parent_session_id": context.session_id,
                "reason": reason_value,
            },
        )
        return {
            "ok": True,
            "code": "RECLAIMED",
            "data": {"child_session_id": child_session_id},
        }

    async def _delegate(
        self,
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        task = arguments.get("task")
        if type(task) is not str or not task.strip():
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "data": {"task": task},
                "error": "task 必须是非空字符串",
            }
        if self.is_subagent(context.session_id):
            return {
                "ok": False,
                "code": "DELEGATION_NOT_ALLOWED",
                "data": {"session_id": context.session_id},
                "error": "子 Agent 不能再委派",
            }
        intent = DelegateIntent(
            context.command_id,
            context.session_id,
            child_session_id(context.session_id, context.command_id),
            task.strip(),
            arguments.get("resolve_conflicts_of"),
        )
        if intent.resolve_conflicts_of is not None:
            events = await self._require_runtime().snapshot(context.session_id)
            source = next((item for item in project_delegate_intents(events)
                           if item.command_id == intent.resolve_conflicts_of), None)
            if source is None or source.child_session_id not in project_reclaimed(events):
                return {"ok": False, "code": "UNKNOWN_CONFLICT_SOURCE", "data": {},
                        "error": "解冲突只能引用当前会话已经交回的委派。"}
            available = await self._transport("child_workspace_version", source.child_session_id,
                                              {"parent_session_id": context.session_id})
            if not available["ok"]:
                return available
        await self._transport(
            "create_child",
            intent.child_session_id,
            task_fact_arguments(intent),
        )
        self._visible_pending.setdefault(context.session_id, set()).add(
            intent.child_session_id
        )
        self._publish_activity(context.session_id)
        return _delegated_result(intent.child_session_id)

    async def _report(
        self,
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        summary = arguments.get("summary")
        if type(summary) is not str or not summary.strip():
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "data": {"summary": summary},
                "error": "summary 必须是非空字符串",
            }
        return {
            "ok": True,
            "code": "REPORTED",
            "data": {"summary": summary.strip()},
        }

    def _require_runtime(self) -> AgentRuntime:
        if self._runtime is None:
            raise RuntimeError("SubAgentHost 尚未绑定 Runtime")
        return self._runtime
