from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from konden.automation.routines import ACTIVE, DELETED, PAUSED
from konden.automation.tool import SessionTransport
from konden.runtime import (
    CommandOutcome,
    CommandOutcomeReceived,
    EventDraft,
    OutcomeStatus,
    SqliteJournal,
    ToolBinding,
    replay,
)
from konden.runtime.dispatcher import AttemptContext
from konden.runtime.model import CommandPhase


SAVE_ROUTINE = "save_routine"
SET_ROUTINE_STATE = "set_routine_state"
LIST_ROUTINES = "list_routines"
ROUTINE_TOOLS = (SAVE_ROUTINE, SET_ROUTINE_STATE, LIST_ROUTINES)

ROUTINE_SCHEMAS: list[dict[str, object]] = [
    {"type": "function", "function": {
        "name": SAVE_ROUTINE,
        "description": (
            "保存一个例行任务：按时间表到点叫醒你，把 intent 原样交还给你，由你当时判断怎么做。"
            "适合长期、重复的职责，例如定时检查 PR 与 CI。intent 写给以后的自己，写目标与判断规则，"
            "不写死具体的工具调用；做法稳定的流程放进 Skill，intent 里说明按哪个 Skill 做。"
            "不带 routine_id 时新建；带上已有的 routine_id 时改写它，保留历史，下一次运行按新时间表重新计算。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "routine_id": {"type": "string", "minLength": 1,
                               "description": "改写已有例行任务时填写；新建时省略。"},
                "name": {"type": "string", "minLength": 1, "maxLength": 80, "description": "简短的名字。"},
                "intent": {"type": "string", "minLength": 1, "maxLength": 4000,
                           "description": "每次到点时要做的事与判断规则。"},
                "schedule": {"type": "string", "minLength": 1, "description": (
                    "5 段 cron，按本机本地时间，例如 '*/10 9-18 * * 1-5'；或 '@every 30m'、'@every 2h'、"
                    "'@every 1d'。两次运行至少间隔 5 分钟。")},
            },
            "required": ["name", "intent", "schedule"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": SET_ROUTINE_STATE,
        "description": (
            "暂停、恢复或删除你的一个例行任务。暂停后不再按时叫醒你，恢复后从现在起重新计算下一次运行；"
            "有条件的盯梢在完成后应删除。人暂停的例行任务只能由人恢复。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "routine_id": {"type": "string", "minLength": 1},
                "state": {"type": "string", "enum": [ACTIVE, PAUSED, DELETED]},
            },
            "required": ["routine_id", "state"],
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": LIST_ROUTINES,
        "description": "列出你的例行任务：名字、intent、时间表、状态、下一次与上一次运行时间。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
]


def _invalid(error: str) -> dict[str, object]:
    return {"ok": False, "code": "INVALID_ARGUMENT", "error": error}


async def request_routine(
    transport: SessionTransport, name: str, session_id: str, command_id: str, arguments: Mapping[str, object],
) -> dict[str, object]:
    arguments = dict(arguments)
    if name == LIST_ROUTINES:
        if arguments:
            return _invalid("list_routines 不接受参数。")
        return await transport(LIST_ROUTINES, session_id, {})
    if name == SAVE_ROUTINE:
        if not {"name", "intent", "schedule"} <= set(arguments) <= {"routine_id", "name", "intent", "schedule"}:
            return _invalid("save_routine 需要 name、intent、schedule，可选 routine_id。")
        if any(type(value) is not str for value in arguments.values()):
            return _invalid("save_routine 的参数都是字符串。")
        return await transport(SAVE_ROUTINE, session_id, {"command_id": command_id, "routine_id": None, **arguments})
    if set(arguments) != {"routine_id", "state"} or arguments["state"] not in (ACTIVE, PAUSED, DELETED) \
            or type(arguments["routine_id"]) is not str:
        return _invalid("set_routine_state 需要 routine_id，以及 state 为 active、paused 或 deleted。")
    return await transport(SET_ROUTINE_STATE, session_id, {"command_id": command_id, **arguments})


def routine_bindings(transport: SessionTransport) -> dict[str, ToolBinding]:
    def binding(name):
        async def handler(context: AttemptContext, arguments: Mapping[str, object]) -> object:
            return await request_routine(transport, name, context.session_id, context.command_id, arguments)
        return ToolBinding(handler)
    return {name: binding(name) for name in ROUTINE_TOOLS}


async def recover_routine_attempts(journal: SqliteJournal, session_id: str, transport: SessionTransport) -> None:
    """Routine requests are keyed by their Command, so an unknown attempt is replayed for its result."""
    events = await journal.snapshot(session_id)
    state = replay(session_id, events).state
    for item in state.commands:
        if item.command.effect.name not in ROUTINE_TOOLS or item.phase is not CommandPhase.UNKNOWN:
            continue
        (attempt,) = item.attempts
        result = await request_routine(transport, item.command.effect.name, session_id,
                                       item.command.command_id, item.command.effect.argument_dict())
        await journal.record_attempt_fact(EventDraft(
            event_id=f"routine-recovered:{item.command.command_id}",
            session_id=session_id,
            payload=CommandOutcomeReceived(item.command.command_id, attempt.attempt_id,
                                           CommandOutcome(OutcomeStatus.SUCCEEDED, value=result)),
            occurred_at=datetime.now(timezone.utc),
            causation_id=attempt.started_event_id,
        ))
