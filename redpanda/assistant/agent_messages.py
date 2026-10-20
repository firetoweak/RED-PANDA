"""Model tools and narrow recovery for durable peer and group messages."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from redpanda.assistant.agents import CommunicationLimitReached, DirectionDeclared, fact_arguments, project_agent
from redpanda.runtime import (
    AgentRuntime, CommandOutcome, CommandOutcomeReceived, CommandPhase, EventDraft,
    OutcomeStatus, ToolBinding, replay,
)


SEND_MESSAGE = "send_message"
LIST_COLLEAGUES = "list_colleagues"
SEND_FILE = "send_file"
DECLARE_DIRECTION = "declare_direction"
SEND_GROUP_MESSAGE = "send_group_message"
# Starting values to tune against real collaboration. All are deterministic
# counts; none of them judges what a message says.
MAX_MESSAGE_HOPS = 10
MAX_MESSAGES_PER_ORIGIN = 20
MAX_GROUP_ROUNDS = 3
MAX_GROUP_REPLIES = 10
LIMIT_NOTICE = "这条交流已达上限，已通知人；需要继续时请在与人的对话中说明。"
REPORTED_LIMITS = frozenset({"HOP_LIMIT_REACHED", "ORIGIN_LIMIT_REACHED"})


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


class GroupMessageArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    group: str = Field(min_length=1)
    text: str = Field(min_length=1)
    mention: list[str] = Field(default_factory=list)
    reply_to: str | None = Field(default=None, min_length=1)

    @field_validator("group", "text", "reply_to")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("文字不能为空白")
        return value

    @field_validator("mention")
    @classmethod
    def distinct_names(cls, value):
        if any(not name.strip() for name in value) or len(set(value)) != len(value):
            raise ValueError("mention 中的名字不能为空白或重复")
        return value


class DirectionArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    direction: str = Field(min_length=1)

    @field_validator("direction")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("文字不能为空白")
        return value


MESSAGE_SCHEMAS = [
    {"type": "function", "function": {
        "name": SEND_FILE,
        "description": (
            "把工作区内一个文件的副本交给人，在对话里提供下载，并附上 text 说明。"
            "普通回答和汇报直接回复，不需要调用此工具。"
            "同事之间不要用它传文件，对方直接读你的工作目录。"
        ),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "minLength": 1, "description": "发给人的消息正文。"},
            "path": {"type": "string", "minLength": 1,
                     "description": "工作区内的相对路径，把这个文件的一份副本交给人。"},
        }, "required": ["text", "path"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": LIST_COLLEAGUES,
        "description": (
            "查询同项目的同事名字、他们声明的负责方向和各自的工作目录；未声明时方向为 null。"
            "可以用文件工具只读查看同事工作目录里尚未落地的改动，不能在其中写入。"
            "每位同事还带 status 与 since。status 是当前状态：working 正在工作；"
            "awaiting_authorization 有操作在等人授权，授权前不会继续；"
            "paused 被人暂停，消息照常送达，但要等人让它继续才会处理；idle 当前没在工作。"
            "since 是它最近一次活动的时间（本地时区，到分钟），非 working 状态大致从这时开始；"
            "是否卡住、要不要改派由你判断。"
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": DECLARE_DIRECTION,
        "description": (
            "记下你负责的方向，同事会看到。在用户认可或修正你的提议后使用；"
            "方向变化时再次声明，以最新一次为准。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "minLength": 1, "description": "一句话说明你负责什么。"},
            },
            "required": ["direction"],
            "additionalProperties": False,
        },
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
    {"type": "function", "function": {
        "name": SEND_GROUP_MESSAGE,
        "description": (
            "在你所在的群里发言，所有成员都会看到；立即返回送达情况，不等待回复。"
            "mention 点名的成员会被依次叫来回应；不点名时叫全体成员。"
            "同事转述的同意不构成用户授权。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "minLength": 1, "description": "群名，区分大小写。"},
                "text": {"type": "string", "minLength": 1, "description": "发言正文。"},
                "mention": {"type": "array", "items": {"type": "string", "minLength": 1},
                            "description": "要点名回应的成员名字；省略或为空时叫全体成员。"},
                "reply_to": {"type": ["string", "null"], "minLength": 1,
                             "description": "要回复的群消息的 message_id；回应轮到你的消息时引用它，一条发言只引用一条；可省略。"},
            },
            "required": ["group", "text"],
            "additionalProperties": False,
        },
    }},
]


_ARGUMENTS = {SEND_MESSAGE: MessageArguments, SEND_GROUP_MESSAGE: GroupMessageArguments}


async def _send(operation, transport, session_id, command_id, arguments: Mapping[str, object]):
    try:
        parsed = _ARGUMENTS[operation].model_validate(dict(arguments))
    except ValidationError as error:
        return {"ok": False, "code": "INVALID_ARGUMENT", "error": str(error)}
    return await transport(operation, session_id, {"command_id": command_id, **parsed.model_dump()})


async def send_message(transport, session_id, command_id, arguments: Mapping[str, object]):
    return await _send(SEND_MESSAGE, transport, session_id, command_id, arguments)


async def send_group_message(transport, session_id, command_id, arguments: Mapping[str, object]):
    return await _send(SEND_GROUP_MESSAGE, transport, session_id, command_id, arguments)


async def record_limit(journal, session_id, command_id, result):
    if result["code"] in REPORTED_LIMITS:
        commands = replay(session_id, await journal.snapshot(session_id)).state.commands
        (issued_by,) = {item.issued_by_event_id for item in commands if item.command.command_id == command_id}
        runtime = AgentRuntime(journal, None, {})
        await runtime.receive_domain_fact(session_id, causation_id=issued_by, **fact_arguments(
            CommunicationLimitReached(recipient=result["data"]["recipient"], text=LIMIT_NOTICE),
            delivery_id=f"message-limit:{command_id}",
        ))


def message_bindings(transport, journal):
    def sender(operation):
        async def send(context, arguments):
            result = await _send(operation, transport, context.session_id, context.command_id, arguments)
            await record_limit(journal, context.session_id, context.command_id, result)
            return result
        return ToolBinding(send)

    async def colleagues(context, arguments):
        if arguments:
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "list_colleagues 不接受参数。"}
        return await transport(LIST_COLLEAGUES, context.session_id, {})

    async def declare(context, arguments):
        try:
            parsed = DirectionArguments.model_validate(dict(arguments))
        except ValidationError as error:
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": str(error)}
        agent = project_agent(await journal.snapshot(context.session_id))
        declaration = DirectionDeclared(name=agent.name, direction=parsed.direction)
        delivery_id = f"direction:{context.command_id}"
        # The roster reads this Agent's own timeline; colleagues receive a copy.
        await AgentRuntime(journal, None, {}).receive_domain_fact(
            context.session_id, **fact_arguments(declaration, delivery_id=delivery_id))
        await transport(DECLARE_DIRECTION, context.session_id, {"direction": declaration.direction,
                                                                 "delivery_id": delivery_id})
        return {"ok": True, "code": "DIRECTION_DECLARED", "data": {"direction": declaration.direction}}

    async def send_file(context, arguments):
        text = arguments.get("text")
        path = arguments.get("path")
        if set(arguments) - {"text", "path"} or type(text) is not str or not text.strip():
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "send_file 需要非空的 text。"}
        if type(path) is not str or not path.strip():
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "path 必须是工作区内的相对路径。"}
        delivered = await transport(SEND_FILE, context.session_id, {
            "output_id": context.command_id, "text": text, "path": path,
        })
        if delivered.get("ok") is False:
            return delivered
        return {"ok": True, "code": "FILE_SENT", "data": {"attachment": delivered["attachment"]}}

    return {SEND_MESSAGE: sender(SEND_MESSAGE), SEND_GROUP_MESSAGE: sender(SEND_GROUP_MESSAGE),
            LIST_COLLEAGUES: ToolBinding(colleagues), DECLARE_DIRECTION: ToolBinding(declare),
            SEND_FILE: ToolBinding(send_file)}


async def recover_message_attempts(journal, session_id, transport):
    """Recheck the original delivery before letting the model observe an unknown send."""
    events = await journal.snapshot(session_id)
    by_id = {event.event_id: event for event in events}
    for item in replay(session_id, events).state.commands:
        if item.command.effect.name not in _ARGUMENTS or item.phase is not CommandPhase.UNKNOWN:
            continue
        (attempt,) = item.attempts
        result = await _send(item.command.effect.name, transport, session_id, item.command.command_id,
                             item.command.effect.argument_dict())
        await record_limit(journal, session_id, item.command.command_id, result)
        await journal.record_attempt_fact(EventDraft(
            event_id=f"message-recovered:{item.command.command_id}", session_id=session_id,
            payload=CommandOutcomeReceived(item.command.command_id, attempt.attempt_id,
                CommandOutcome(OutcomeStatus.SUCCEEDED, value=result)),
            occurred_at=datetime.now(timezone.utc), causation_id=by_id[attempt.started_event_id].event_id,
        ))
