"""测试用的投影与决策辅助：直接观察协议消息和工具调用请求。"""
from __future__ import annotations

from typing import AbstractSet

from redpanda.assistant.attachments import AttachmentStore
from redpanda.assistant.context.projection import (
    _externalized_meta,
    _translate_visible_events,
)
from redpanda.assistant.context.prompt import DEFAULT_ASSISTANT_PROMPT
from redpanda.assistant.decision import _invoke_requests
from redpanda.llm.api import LLMResponse
from redpanda.runtime import ModelDecision
from redpanda.runtime.events import Event
from redpanda.runtime.model import DecisionState


def project_chat_messages(
    events: tuple[Event, ...],
    state: DecisionState,
    system_prompt: str = DEFAULT_ASSISTANT_PROMPT,
    attachments: AttachmentStore | None = None,
) -> list[dict[str, object]]:
    """把冻结可见 Event 译成模型协议消息，不脱水、不截断。"""
    return [
        item.message
        for item in _translate_visible_events(
            events,
            state,
            system_prompt,
            attachments,
        )
    ]


def parse_tool_result_meta(content: object) -> tuple[bool, str | None]:
    meta = _externalized_meta(content)
    if meta is None:
        return False, None
    return True, meta["reference"]


def decision_from_llm(
    response: LLMResponse,
    allowed_tool_names: AbstractSet[str],
    exclusive_tool_names: AbstractSet[str] = frozenset(),
) -> ModelDecision:
    return ModelDecision(
        content=response.content,
        command_requests=_invoke_requests(
            response.calls,
            allowed_tool_names,
            exclusive_tool_names,
        ),
    )
