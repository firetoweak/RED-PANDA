"""Who a session's closing text is for, decided from the session's identity alone.

A long-lived Agent has many listeners and addresses each through an explicit
tool: ``message_user`` for the person, ``send_message`` for a colleague,
``send_group_message`` for a group. Its closing text is its work log, never a
reply inferred from what started the turn. Other sessions answer the person
directly.
"""

from __future__ import annotations

from typing import Literal

from redpanda.assistant.agents import project_agent
from redpanda.runtime import Event

Audience = Literal["user", "log"]


def audience(events: tuple[Event, ...]) -> Audience:
    return "user" if project_agent(events) is None else "log"
