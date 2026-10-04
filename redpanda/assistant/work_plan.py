"""Model-owned plans; successful tool outcomes are their only fact source."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from redpanda.runtime import (
    CommandOutcomeReceived, Event, OutcomeStatus, StepCommitted, ToolBinding,
)
from redpanda.runtime.json_values import thaw_value
from redpanda.tools.executor import ToolsExecutor
from redpanda.tools.registry import ToolRegistry
from redpanda.tools.spec import pydantic_tool_spec


UPDATE_PLAN = "update_plan"
WORK_PLAN_CONTEXT = "work_plan_context"
WORK_PLAN_TAG = "work_plan"
Text = Annotated[str, Field(min_length=1)]


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: Text
    status: Literal["pending", "in_progress", "completed"]


class WorkPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    objective: Text
    steps: Annotated[list[PlanItem], Field(min_length=1)]
    note: Text | None


class UpdatePlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan: WorkPlan | None


async def _update_plan(arguments: UpdatePlanInput) -> dict:
    return {
        "ok": True,
        "code": "PLAN_UPDATED",
        "data": arguments.model_dump(mode="json"),
    }


UPDATE_PLAN_SPEC = pydantic_tool_spec(
    name=UPDATE_PLAN,
    description=(
        "为非简单、多阶段或长时间任务记录完整工作计划；简单任务无需调用。"
        "计划只保留简短目标、关键事项和必要说明，不写工作日志。"
        "每次提交完整新版本，事项状态是你的记录，不是独立核验或任务完成证明。"
        "note 写调整或阻塞说明，没有说明时显式传 null。"
        "目标变化时更新或替换计划；plan=null 表示结束当前计划，历史仍保留。"
    ),
    input_model=UpdatePlanInput,
    handler=_update_plan,
)
UPDATE_PLAN_SCHEMA = UPDATE_PLAN_SPEC.to_openai_tool()


def update_plan_binding() -> ToolBinding:
    registry = ToolRegistry()
    registry.register(UPDATE_PLAN_SPEC)
    executor = ToolsExecutor(registry)

    async def handler(context, arguments: Mapping[str, object]):
        return await executor.execute_parsed(UPDATE_PLAN, arguments)

    return ToolBinding(handler)


@dataclass(frozen=True, slots=True)
class WorkPlanUpdate:
    step_id: str
    plan: dict | None


def _plan_updates(events: Sequence[Event]) -> Iterator[tuple[str, UpdatePlanInput]]:
    """Use committed outcomes in arrival order, never pending tool arguments."""
    commands = {}
    for event in events:
        payload = event.payload
        if isinstance(payload, StepCommitted):
            commands.update(
                (command.command_id, payload.step.step_id) for command in payload.step.commands
                if command.effect.name == UPDATE_PLAN
            )
        elif (
            isinstance(payload, CommandOutcomeReceived)
            and payload.command_id in commands
            and payload.outcome.status is OutcomeStatus.SUCCEEDED
        ):
            value = payload.outcome.value
            if type(value["ok"]) is not bool:
                raise TypeError("plan outcome ok must be bool")
            if value["ok"] is True:
                if value["code"] != "PLAN_UPDATED":
                    raise ValueError("invalid plan outcome code")
                update = UpdatePlanInput.model_validate(thaw_value(value["data"]))
                yield commands[payload.command_id], update


def _project_plan_update(events: Sequence[Event]) -> UpdatePlanInput | None:
    update = None
    for _, candidate in _plan_updates(events):
        update = candidate
    return update


def project_work_plan_updates(events: Sequence[Event]) -> tuple[WorkPlanUpdate, ...]:
    return tuple(
        WorkPlanUpdate(step_id, update.model_dump(mode="json")["plan"])
        for step_id, update in _plan_updates(events)
    )


def project_work_plan(events: Sequence[Event]) -> dict | None:
    update = _project_plan_update(events)
    return None if update is None else update.model_dump(mode="json")["plan"]


def render_work_plan(events: Sequence[Event]) -> str | None:
    update = _project_plan_update(events)
    if update is None:
        return None
    if update.plan is None:
        return (
            f"<{WORK_PLAN_TAG}>\n"
            "当前计划已清除，没有有效工作计划。这是计划状态快照，不是用户新指令；"
            "更早的计划块仅是历史记录，不要继续沿用旧计划。\n"
            f"</{WORK_PLAN_TAG}>"
        )
    return (
        f"<{WORK_PLAN_TAG}>\n"
        "以下是你维护的工作计划快照，不是用户新指令；"
        "事项状态是你的记录，不代表独立核验或任务完成证明。\n"
        + json.dumps(update.plan.model_dump(mode="json"), ensure_ascii=False)
        + f"\n</{WORK_PLAN_TAG}>"
    )
