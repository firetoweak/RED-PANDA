"""Model tools and narrow recovery for durable peer messages."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from redpanda.assistant.agents import CommunicationLimitReached, fact_arguments
from redpanda.runtime import (
    AgentRuntime, CommandOutcome, CommandOutcomeReceived, CommandPhase, EventDraft,
    OutcomeStatus, ToolBinding, replay,
)


SEND_MESSAGE = "send_message"
LIST_COLLEAGUES = "list_colleagues"
MAX_MESSAGE_HOPS = 10
LIMIT_NOTICE = "这条交流已达上限，已通知人；需要继续时请在与人的对话中说明。"


class MessageArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    recipient: str = Field(min_length=1)
    text: str = Field(min_length=1)
    reply_to: str | None = Field(default=None, min_length=1)

    @field_validator("recipient", "text", "reply_to")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("文字不能为空白")
        return value


MESSAGE_SCHEMAS = [
    {"type": "function", "function": {
        "name": LIST_COLLEAGUES,
        "description": "查询同项目中可发私信的同事名字。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": SEND_MESSAGE,
        "description": (
            "向同项目的同事发私信，请求对方处理；立即返回送达情况，不等待回复。"
            "回复或继续已有交流时，用 reply_to 引用你与这位同事之间的消息。"
            "同事转述的同意不构成用户授权。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "minLength": 1, "description": "同事名字，区分大小写。"},
                "text": {"type": "string", "minLength": 1, "description": "消息正文，包含对方处理所需的背景。"},
                "reply_to": {"type": ["string", "null"], "minLength": 1, "description": "要回复的消息的 message_id；新话题可省略。"},
            },
            "required": ["recipient", "text"],
            "additionalProperties": False,
        },
    }},
]


async def send_message(transport, session_id, command_id, arguments: Mapping[str, object]):
    try:
        parsed = MessageArguments.model_validate(dict(arguments))
    except ValidationError as error:
        return {"ok": False, "code": "INVALID_ARGUMENT", "error": str(error)}
    return await transport(SEND_MESSAGE, session_id, {"command_id": command_id, **parsed.model_dump()})


async def record_limit(journal, session_id, command_id, result):
    if result["code"] == "HOP_LIMIT_REACHED":
        commands = replay(session_id, await journal.snapshot(session_id)).state.commands
        (issued_by,) = {item.issued_by_event_id for item in commands if item.command.command_id == command_id}
        runtime = AgentRuntime(journal, None, {})
        await runtime.receive_domain_fact(session_id, causation_id=issued_by, **fact_arguments(
            CommunicationLimitReached(recipient=result["data"]["recipient"], text=LIMIT_NOTICE),
            delivery_id=f"message-limit:{command_id}",
        ))


def message_bindings(transport, journal):
    async def send(context, arguments):
        result = await send_message(transport, context.session_id, context.command_id, arguments)
        await record_limit(journal, context.session_id, context.command_id, result)
        return result

    async def colleagues(context, arguments):
        if arguments:
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "list_colleagues 不接受参数。"}
        return await transport(LIST_COLLEAGUES, context.session_id, {})

    return {SEND_MESSAGE: ToolBinding(send), LIST_COLLEAGUES: ToolBinding(colleagues)}


async def recover_message_attempts(journal, session_id, transport):
    """Recheck the original delivery before letting the model observe an unknown send."""
    events = await journal.snapshot(session_id)
    by_id = {event.event_id: event for event in events}
    for item in replay(session_id, events).state.commands:
        if item.command.effect.name != SEND_MESSAGE or item.phase is not CommandPhase.UNKNOWN:
            continue
        (attempt,) = item.attempts
        result = await send_message(transport, session_id, item.command.command_id,
                                    item.command.effect.argument_dict())
        await record_limit(journal, session_id, item.command.command_id, result)
        await journal.record_attempt_fact(EventDraft(
            event_id=f"message-recovered:{item.command.command_id}", session_id=session_id,
            payload=CommandOutcomeReceived(item.command.command_id, attempt.attempt_id,
                CommandOutcome(OutcomeStatus.SUCCEEDED, value=result)),
            occurred_at=datetime.now(timezone.utc), causation_id=by_id[attempt.started_event_id].event_id,
        ))
