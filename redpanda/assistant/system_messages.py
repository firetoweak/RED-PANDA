"""System-origin input, independent of the component that produced it."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from redpanda.runtime import DomainFactCommitted, UserMessageReceived, StepCommitted
from redpanda.runtime.json_values import thaw_value


SYSTEM_MESSAGE = "assistant.system_message"


class SystemMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1)
    severity: Literal["info", "warning"]
    related_session_id: str | None

    def model_text(self) -> str:
        source = "系统运行提醒，不是用户新指令。"
        related = "" if self.related_session_id is None else f"\n相关子任务：{self.related_session_id}"
        return f"<system_notice>\n{source}{related}\n{self.text}\n</system_notice>"


def system_message(event) -> SystemMessage | None:
    payload = event.payload
    if not isinstance(payload, DomainFactCommitted) or payload.fact_type != SYSTEM_MESSAGE:
        return None
    return SystemMessage.model_validate(thaw_value(payload.data))


def current_system_message(events) -> dict | None:
    current = None
    for event in events:
        if isinstance(event.payload, (UserMessageReceived, StepCommitted)):
            current = None
        message = system_message(event)
        if message is not None:
            current = message.model_dump()
    return current
