"""SubAgent 委派、父子交流与回收。

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

from redpanda.assistant.agents import project_agent
from redpanda.assistant.context.prompt import SUBAGENT_PROMPT, SUBAGENT_READ_ONLY_PROMPT
from redpanda.assistant.delivery import DELIVER_TOOL_NAME, DeliverySink, deliver_binding, emit_delivery
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
INSTRUCT = "instruct_subagent"
LIST_SUBAGENTS = "list_subagents"

TASK_FACT = "subagent.task"
INSTRUCTION_FACT = "subagent.instruction"
REPLY_FACT = "subagent.reply"
CLOSED_FACT = "subagent.closed"
RETURN_FACT = "subagent.return"

FACT_SOURCE = "subagent"


@dataclass(frozen=True, slots=True)
class DelegateIntent:
    command_id: str
    parent_session_id: str
    child_session_id: str
    task: str
    resolve_conflicts_of: str | None = None
    project: str | None = None


@dataclass(frozen=True, slots=True)
class SubAgentTask:
    """`project` is the workspace id the task got a copy of; None means it only reads."""
    child_session_id: str
    parent_session_id: str
    delegate_command_id: str
    task: str
    resolve_conflicts_of: str | None = None
    project: str | None = None


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
            "project": intent.project,
        },
        "delivery_id": f"{intent.command_id}:task",
        "source": FACT_SOURCE,
        "requests_decision": True,
    }


def _task_from_parts(
    child_id: str,
    data: Mapping[str, object],
    *,
    delivery_id: str,
    source: str,
    requests_decision: object,
) -> SubAgentTask:
    """Parse self-written task identity; contract breaks raise, no field-set walls."""

    if source != FACT_SOURCE:
        raise ValueError("subagent task source 无效")
    if requests_decision is not True:
        raise ValueError("subagent task 必须请求首次决策")
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
    if delivery_id != f"{command_id}:task":
        raise ValueError("subagent task delivery identity 无效")
    resolve = data["resolve_conflicts_of"]
    if resolve is not None and (type(resolve) is not str or not resolve):
        raise ValueError("resolve_conflicts_of 必须是非空 string|null")
    project = data["project"]
    if project is not None and (type(project) is not str or not project):
        raise ValueError("project 必须是非空 string|null")
    if resolve is not None and project is None:
        raise ValueError("resolving conflicts needs a project copy")
    return SubAgentTask(child_id, parent_session_id, command_id, task, resolve, project)


def task_from_arguments(
    child_id: str,
    arguments: Mapping[str, object],
) -> SubAgentTask:
    if arguments["fact_type"] != TASK_FACT:
        raise ValueError("subagent task fact_type 无效")
    return _task_from_parts(
        child_id,
        arguments["data"],
        delivery_id=arguments["delivery_id"],
        source=arguments["source"],
        requests_decision=arguments["requests_decision"],
    )


def return_data(
    child_session_id: str,
    *,
    failure: str | None,
    reason: str | None = None,
) -> dict[str, object]:
    return {
        "child_session_id": child_session_id,
        "failure": failure,
        "reason": reason,
    }


def closed_arguments(
    session_id: str,
    data: Mapping[str, object],
) -> dict[str, object]:
    """Only failures wake the parent; it already knows its own close decision."""

    return dict(
        fact_type=CLOSED_FACT,
        data=dict(data),
        delivery_id=f"{session_id}:closed",
        source=FACT_SOURCE,
        requests_decision=data["failure"] is not None,
    )


def instruction_arguments(parent_session_id: str, command_id: str, text: str) -> dict[str, object]:
    return dict(
        fact_type=INSTRUCTION_FACT,
        data={"parent_session_id": parent_session_id, "text": text},
        delivery_id=f"{command_id}:instruction",
        source=FACT_SOURCE,
        requests_decision=True,
    )


def reply_arguments(child_id: str, command_id: str, text: str) -> dict[str, object]:
    return dict(
        fact_type=REPLY_FACT,
        data={"child_session_id": child_id, "text": text},
        delivery_id=f"{child_id}:{command_id}:reply",
        source=FACT_SOURCE,
        requests_decision=True,
    )


def instructed_result(child_id: str) -> dict[str, object]:
    return {"ok": True, "code": "SUBAGENT_INSTRUCTED", "data": {"child_session_id": child_id}}


def child_finished_result(child_id: str) -> dict[str, object]:
    return {"ok": False, "code": "CHILD_FINISHED", "data": {"child_session_id": child_id},
            "error": "子 Agent 的协作已结束，不能追加指令。需要时重新 delegate。"}


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
            failure=ProcessFailure.capture(error).render(),
        ),
    )
    return parent, closed_arguments(session_id, returned.payload.data)


async def record_interrupted_return(journal: Journal, session_id: str) -> None:
    """新 Worker 独占接管后，把未完成调用作为中断事实交回父会话。"""
    events = await journal.snapshot(session_id)
    if project_parent(events) is None or _return_event(events) is not None:
        return
    state = StateProjector().project(session_id, events).state
    if not any(command.phase is CommandPhase.UNKNOWN for command in state.commands):
        return
    await persist_return(
        journal,
        session_id,
        return_data(
            session_id,
            failure="子 Agent 执行中断，存在未取得结果的操作。需要时重新 delegate。",
        ),
    )


SubAgentActivitySink = Callable[[str, bool], None]


CHILD_BUILTIN_TOOL_NAMES = frozenset(
    {
        "glob",
        "grep",
        "read_file",
        "read_content",
        "find_history",
        "load_skill",
        "read_skill_resource",
        "write_file",
        "apply_patch",
        "replace_all",
        "update_plan",
    }
)
"""子 Session 能看见的内建工具；外部工具从只读 Server 目录渐进加载。

显式列举：新增任何工具默认进不来，要进必须有人明确加。
`execute_command` 不在其中——执行等进程沙箱。`DELEGATE` 也不在其中，
递归委派因此被同一份名单挡住。
"""
# A task delegated by a plain session reads the person's own directory and changes nothing.
READ_ONLY_CHILD_TOOL_NAMES = CHILD_BUILTIN_TOOL_NAMES - {"write_file", "apply_patch", "replace_all"}


# A Bot delegates into one of its projects from its committed branch and merges back
# without approval; without a project the task only reads the Bot's own directory.
DELEGATE_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": DELEGATE,
        "description": (
            "把可独立完成的任务交给子 Agent。给出 project 时，它从你在这个项目分支上已提交的工作开始，"
            "在自己的文件副本里工作，看不到未提交的修改，需要时先提交再委派；可以改文件，不能执行命令。"
            "不给 project 时，它只能读取你的个人目录，不能修改。"
            "调用立即返回 child_session_id，子普通回复会直接送给你；由你判断成果是否满足目标。"
            "改了文件的子 Agent 用 compare_subagent 验收，用 merge_subagent 合入你在这个项目的分支。"
            "互不依赖的任务可在同一次决策里各发一次 delegate 并行，结论逐个返回；改同一批文件会在合入时冲突。"
            "询问进展、补充要求或让等待中的子继续时用 instruct_subagent；用 list_subagents 查看是否正在执行。"
            "子普通回复不结束协作；完成验收或不再需要时，用 reclaim 明确结束。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "交给子 Agent 的完整任务描述。它看不到当前对话，"
                        "所需背景必须写在这里。写明只调查还是要修改，以及允许改动的范围。"
                        "路径先自己核实再写进来，写子 Agent 副本里的相对路径；写明找不到目标时如实报告，不要换目标。"
                    ),
                },
                "project": {
                    "type": ["string", "null"],
                    "description": "可选：你挂上的项目名。子 Agent 要改这个项目的文件时必须给出。",
                },
                "resolve_conflicts_of": {
                    "type": ["string", "null"],
                    "description": (
                        "可选：合入冲突的那次 delegate 调用 id；先结束原协作，project 与那次相同；"
                        "新的子 Agent 在带冲突标记的文件上工作，在 task 里说明取舍原则。"
                    ),
                },
            },
            "required": ["task"],
        },
    },
}


DELEGATE_READ_ONLY_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": DELEGATE,
        "description": (
            "把可独立完成的调查交给子 Agent。它只能读取你的工作区，不能修改文件，也不能执行命令。"
            "调用立即返回 child_session_id，子普通回复会直接送给你；由你判断成果是否满足目标。"
            "互不依赖的调查可在同一次决策里各发一次 delegate 并行，结论逐个返回。"
            "询问进展、补充要求或让等待中的子继续时用 instruct_subagent；用 list_subagents 查看是否正在执行。"
            "子普通回复不结束协作；完成验收或不再需要时，用 reclaim 明确结束。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "交给子 Agent 的完整调查任务。它看不到当前对话，所需背景必须写在这里。"
                        "路径先自己核实再写进来；写明找不到目标时如实报告，不要换目标。"
                    ),
                },
            },
            "required": ["task"],
        },
    },
}


INSTRUCT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": INSTRUCT,
        "description": (
            "给自己委派、尚未结束的子 Agent 追加指令、询问进展或调整方向。"
            "打断它当前的思考，保留历史与工作目录，按新指令继续；已发生的改动不会回退，"
            "尚未返回的工具先收尾。返回接纳结果，不等待答复。"
            "子普通回复会送给你，回复或等待消息不结束协作，由你判断继续还是用 reclaim 结束。"
            "协作已经结束的子 Agent 不能继续，需要时重新 delegate。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "child_session_id": {"type": "string", "minLength": 1,
                                     "description": "先前 delegate 返回的 child_session_id。"},
                "text": {"type": "string", "minLength": 1,
                         "description": "完整的新指令与所需背景；子 Agent 看不到你的当前对话。"},
            },
            "required": ["child_session_id", "text"],
            "additionalProperties": False,
        },
    },
}


RECLAIM_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": RECLAIM,
        "description": (
            "结束自己与一个子 Agent 的协作。验收完成、不再需要或任务取消时使用。"
            "停止它的执行并保留历史和文件改动，不自动合入成果；结束后不能继续发指令，需要时重新 delegate。"
            "重复结束同一个子 Agent 没有效果。"
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


LIST_SUBAGENTS_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": LIST_SUBAGENTS,
        "description": "查看自己委派的子 Agent 及其当前状态：creating（创建中）、running（正在执行）、waiting（等待消息）、paused（暂停）、closed（协作结束）、failed（执行失败）。状态不代表任务完成程度；需要进展时直接用 instruct_subagent 询问。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


def project_delegate_intents(
    events: Sequence[Event],
) -> tuple[DelegateIntent, ...]:
    """从父 Step 已提交的 delegate Command 派生全部 child identity。"""

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
                intents.append(
                    DelegateIntent(
                        command.command_id,
                        event.session_id,
                        child_session_id(event.session_id, command.command_id),
                        task.strip(),
                        arguments.get("resolve_conflicts_of"),
                    )
                )
    return tuple(intents)


def _delegated_project(events: Sequence[Event], command_id: str) -> object:
    """The project name a committed delegate call asked for."""
    for event in events:
        if isinstance(event.payload, StepCommitted):
            for command in event.payload.step.commands:
                if command.command_id == command_id:
                    return command.effect.argument_dict().get("project")
    return None


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
            if type(value["code"]) is not str or not value["code"]:
                raise ValueError("delegate failure outcome code 无效")
            if type(value["error"]) is not str or not value["error"]:
                raise ValueError("delegate failure outcome error 无效")
            failed.add(intent.child_session_id)
            continue
        if value["code"] != "DELEGATED":
            raise ValueError("delegate success outcome code 无效")
    return frozenset(failed)


def project_task(events: Sequence[Event]) -> SubAgentTask | None:
    """从子 Journal 读取唯一的委派身份（与 task_from_arguments 共用解析）。"""

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
    delivery = event.delivery
    if delivery is None:
        raise ValueError("subagent task delivery identity 无效")
    return _task_from_parts(
        event.session_id,
        event.payload.data,
        delivery_id=delivery.delivery_id,
        source=delivery.source,
        requests_decision=event.payload.requests_decision,
    )


def project_parent(events: Sequence[Event]) -> str | None:
    """从一条 Session 自己的 Journal 判断它是不是子 Session。"""

    task = project_task(events)
    return None if task is None else task.parent_session_id


def project_pending(events: Sequence[Event]) -> frozenset[str]:
    """父已委派且尚未结束协作的子 Session。"""

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
    failure: str | None
    reason: str | None


def project_unreported_returns(
    events: Sequence[Event],
    returns_by_child: Mapping[str, Mapping[str, object]],
) -> tuple[UnreportedChildReturn, ...]:
    """待回收集合里，子 Journal 已经有回传的那些。"""

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
                data["failure"],
                data["reason"],
            )
        )
    return tuple(sorted(found, key=lambda item: item.command_id))


def project_reclaimed(events: Sequence[Event]) -> frozenset[str]:
    """父 Session 已经收到结束通知的子 Session。"""

    reclaimed: set[str] = set()
    for event in events:
        payload = event.payload
        if (
            not isinstance(payload, DomainFactCommitted)
            or payload.fact_type != CLOSED_FACT
        ):
            continue
        data = payload.data
        if not isinstance(data, Mapping):
            raise ValueError("subagent 回收事实 data 无效")
        reclaimed.add(data["child_session_id"])
    return frozenset(reclaimed)


class SubAgentHost:
    """委派、父子交流、回收，以及子 Session 的策略边界。"""

    def __init__(
        self,
        activity_sink: SubAgentActivitySink | None = None,
        *,
        agent_branch: bool = False,
        projects: bool = False,
    ) -> None:
        # agent_branch: this child runs in a project copy; projects: this parent can delegate into one.
        self._agent_branch = agent_branch
        self._projects = projects
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

        if not self.is_subagent(session_id):
            return None
        return CHILD_BUILTIN_TOOL_NAMES if self._agent_branch else READ_ONLY_CHILD_TOOL_NAMES

    def system_prompt(self, session_id: str) -> str | None:
        if not self.is_subagent(session_id):
            return None
        return SUBAGENT_PROMPT if self._agent_branch else SUBAGENT_READ_ONLY_PROMPT

    def schemas(self, session_id: str) -> list[dict[str, object]]:
        if self.is_subagent(session_id):
            return []
        return [DELEGATE_SCHEMA if self._projects else DELEGATE_READ_ONLY_SCHEMA, INSTRUCT_SCHEMA, LIST_SUBAGENTS_SCHEMA, RECLAIM_SCHEMA]

    def bindings(self) -> dict[str, ToolBinding]:
        return {
            DELEGATE: ToolBinding(self._delegate, decision_on_outcome=False),
            INSTRUCT: ToolBinding(self._instruct_command),
            RECLAIM: ToolBinding(self._reclaim_command),
            LIST_SUBAGENTS: ToolBinding(self._list_subagents),
        }

    async def note_returned(self, session_id: str) -> bool:
        """A child with subagent.return has no more work. Do not wake it."""

        if session_id in self._returned:
            return True
        if _return_event(await self._require_runtime().snapshot(session_id)) is None:
            return False
        self._returned.add(session_id)
        return True

    def delivery_binding(self, sink: DeliverySink, observation_sink=None, preview=None):
        """A child's ordinary speech is a durable message to its parent."""
        async def routed(session_id, output_id, text):
            target = observation_sink if self.is_subagent(session_id) else sink
            if target is not None:
                await emit_delivery(target, session_id, output_id, text)

        binding = deliver_binding(routed, preview)[DELIVER_TOOL_NAME]

        async def handler(context, arguments):
            if self.is_subagent(context.session_id):
                await self.send_reply(context, arguments["text"])
            return await binding.handler(context, arguments)

        return {DELIVER_TOOL_NAME: ToolBinding(handler, decision_on_outcome=False)}

    async def send_reply(self, context, text):
        return await self._transport("child_message", context.session_id, {
            "command_id": context.command_id, "text": text,
        })

    async def _list_subagents(self, context, arguments):
        if arguments:
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "此工具不接受参数。"}
        return await self._transport("list_subagents", context.session_id, {})

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
                        closed_arguments(session_id, event.payload.data),
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
            self._parents[intent.child_session_id] = intent.parent_session_id
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
        failure: str | None,
        reason: str | None = None,
    ) -> None:
        """把一个子 Session 的终局交回父。一个子最多回收一次。

        回收事实只记这个子自己的终局。「还差谁」是派生值，由父在决策时从自己
        已冻结的事实里投影；冻进事实就要求两个并行的子在父维度串行读写。
        已经有终局时沿用先写入的那条，包括父结束协作与子执行失败的竞态。
        """

        returned = await persist_return(
            self._require_runtime()._journal,
            session_id,
            return_data(
                session_id,
                failure=failure,
                reason=reason,
            ),
        )
        await self._transport(
            "fact",
            parent_session_id,
            closed_arguments(session_id, returned.payload.data),
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

    async def _instruct_command(self, context: AttemptContext, arguments: Mapping[str, object]):
        if self.is_subagent(context.session_id):
            return {"ok": False, "code": "INSTRUCT_NOT_ALLOWED", "error": "子 Agent 不能指挥其他子 Agent。"}
        child_id = arguments.get("child_session_id")
        text = arguments.get("text")
        if (set(arguments) != {"child_session_id", "text"}
                or type(child_id) is not str or not child_id.strip()
                or type(text) is not str or not text.strip()):
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "需要非空的 child_session_id 和 text。"}
        child_id, text = child_id.strip(), text.strip()
        events = await self._require_runtime().snapshot(context.session_id)
        if child_id not in project_delegations(events) or child_id in project_failed_delegations(events):
            return {"ok": False, "code": "UNKNOWN_CHILD", "error": "不是当前会话委派的子 Agent。"}
        return await self._transport("instruct_child", child_id, {
            "parent_session_id": context.session_id, "command_id": context.command_id, "text": text,
        })

    async def recover_messages(self, session_id: str) -> None:
        """Replay known instructions and child speech with their original identity."""
        runtime = self._require_runtime()
        state = await runtime.state(session_id)
        for command in state.commands:
            name = command.command.effect.name
            if command.phase is not CommandPhase.UNKNOWN:
                continue
            if name != INSTRUCT and not (name == DELIVER_TOOL_NAME and self.is_subagent(session_id)):
                continue
            attempt = command.attempts[-1]
            context = AttemptContext(session_id, command.command.command_id, attempt.attempt_id, len(command.attempts))
            arguments = command.command.effect.argument_dict()
            if name == INSTRUCT:
                result = await self._instruct_command(context, arguments)
            else:
                await self.send_reply(context, arguments["text"])
                result = arguments["text"]
            await runtime.dispatcher.accept_outcome(
                session_id, command.command.command_id, attempt.attempt_id, attempt.started_event_id,
                CommandOutcome(OutcomeStatus.SUCCEEDED, value=result),
            )

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
        events = await self._require_runtime().snapshot(context.session_id)
        project = arguments.get("project")
        workspace_id = None
        if project is not None:
            agent = project_agent(events)
            linked = None if agent is None else agent.project(project)
            if linked is None:
                return {"ok": False, "code": "PROJECT_NOT_LINKED",
                        "data": {"projects": [] if agent is None else [item.name for item in agent.projects]},
                        "error": "你这里没有挂这个项目。", "hint": "用 data.projects 里的名字，或不给 project 只做只读调查。"}
            workspace_id = linked.workspace_id
        intent = DelegateIntent(
            context.command_id,
            context.session_id,
            child_session_id(context.session_id, context.command_id),
            task.strip(),
            arguments.get("resolve_conflicts_of"),
            workspace_id,
        )
        if intent.resolve_conflicts_of is not None:
            source = next((item for item in project_delegate_intents(events)
                           if item.command_id == intent.resolve_conflicts_of), None)
            if (source is None or source.child_session_id not in project_reclaimed(events)
                    or project is None or _delegated_project(events, source.command_id) != project):
                return {"ok": False, "code": "UNKNOWN_CONFLICT_SOURCE", "data": {},
                        "error": "解冲突只能引用当前会话已经结束协作、并且 project 相同的委派。"}
            available = await self._transport("prepare_child_files", source.child_session_id,
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

    def _require_runtime(self) -> AgentRuntime:
        if self._runtime is None:
            raise RuntimeError("SubAgentHost 尚未绑定 Runtime")
        return self._runtime
