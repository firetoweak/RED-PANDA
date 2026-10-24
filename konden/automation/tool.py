from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from konden.automation.once import MAX_DELAY_SECONDS
from konden.runtime import ToolBinding
from konden.runtime.dispatcher import AttemptContext


SCHEDULE_ONCE = "schedule_once"
CANCEL_SCHEDULE = "cancel_schedule"

SCHEDULE_ONCE_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": SCHEDULE_ONCE,
        "description": (
            "安排一次稍后的提醒，调用立即返回。到时你会收到带计划时间、实际时间和 purpose 的提醒，"
            "再判断是否检查、回答或重新安排；用返回的 schedule_id 可在触发前取消。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delay_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_DELAY_SECONDS,
                    "description": "从现在起多少秒后触发一次。",
                },
                "purpose": {
                    "type": "string",
                    "minLength": 1,
                    "description": "到时你要重新判断的事项；到点时它会原样送回给你，不会被自动执行。",
                },
            },
            "required": ["delay_seconds", "purpose"],
            "additionalProperties": False,
        },
    },
}


CANCEL_SCHEDULE_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": CANCEL_SCHEDULE,
        "description": (
            "取消尚未触发的提醒；重复取消仍返回成功；已触发的返回 ALREADY_FIRED，提醒仍会到达。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "schedule_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "schedule_once 返回的 schedule_id。",
                },
            },
            "required": ["schedule_id"],
            "additionalProperties": False,
        },
    },
}


SessionTransport = Callable[[str, str, dict], Awaitable[object]]


def schedule_once_binding(transport: SessionTransport) -> ToolBinding:
    async def schedule_once(
        context: AttemptContext, arguments: Mapping[str, object]
    ) -> object:
        return await register_schedule(
            transport, context.session_id, context.command_id, arguments
        )

    return ToolBinding(schedule_once)


def cancel_schedule_binding(transport: SessionTransport) -> ToolBinding:
    async def handler(context: AttemptContext, arguments: Mapping[str, object]) -> object:
        return await cancel_schedule(
            transport, context.session_id, context.command_id, arguments
        )

    return ToolBinding(handler)


async def cancel_schedule(
    transport: SessionTransport,
    session_id: str,
    command_id: str,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    if (
        set(arguments) != {"schedule_id"}
        or type(arguments["schedule_id"]) is not str
        or not arguments["schedule_id"].strip()
    ):
        return {
            "ok": False,
            "code": "INVALID_ARGUMENT",
            "error": "cancel_schedule 需要非空的 schedule_id",
        }
    code = await transport(
        CANCEL_SCHEDULE,
        session_id,
        {"command_id": command_id, "schedule_id": arguments["schedule_id"]},
    )
    return {
        "ok": code == "CANCELLED",
        "code": code,
        "data": {"schedule_id": arguments["schedule_id"]},
    }


async def register_schedule(
    transport: SessionTransport,
    session_id: str,
    command_id: str,
    arguments: Mapping[str, object],
    *,
    started_at: str | None = None,
) -> dict[str, object]:
    try:
        delay_seconds, purpose = _schedule_arguments(arguments)
    except ValueError as error:
        return {"ok": False, "code": "INVALID_ARGUMENT", "error": str(error)}
    request = {
        "schedule_id": command_id,
        "delay_seconds": delay_seconds,
        "purpose": purpose,
    }
    if started_at is not None:
        request["started_at"] = started_at
    result = await transport(SCHEDULE_ONCE, session_id, request)
    return {"ok": True, "code": "SCHEDULED", "data": result}


def _schedule_arguments(arguments: Mapping[str, object]) -> tuple[int, str]:
    if set(arguments) != {"delay_seconds", "purpose"}:
        raise ValueError("schedule_once 只接受 delay_seconds 和 purpose 两个参数")
    delay_seconds = arguments["delay_seconds"]
    purpose = arguments["purpose"]
    if type(delay_seconds) is not int or not 1 <= delay_seconds <= MAX_DELAY_SECONDS:
        raise ValueError(f"delay_seconds 必须在 1 到 {MAX_DELAY_SECONDS} 之间")
    if type(purpose) is not str or not purpose.strip():
        raise ValueError("purpose 必须是非空字符串")
    return delay_seconds, purpose
