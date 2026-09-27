from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from helperme.assistant.host.supervisor import HostSupervisor
from helperme.assistant.session_metadata import SessionFlagStore, SessionLineageStore


def host_watching(order):
    host = object.__new__(HostSupervisor)
    host._pause = SessionFlagStore(None, "paused.json")
    host._lineage = SessionLineageStore(None, "lineage.json")
    host._with_host_metadata = lambda observed, session_id: observed
    host.locks = {}

    async def application(operation, session_id, arguments):
        order.append(f"{operation}:{session_id}")
        return "view"

    async def fork_after_turn(source, user_message_id, child):
        order.append(f"fork_turn:{user_message_id}")

    host.compact = SimpleNamespace(application=AsyncMock(side_effect=application))
    host.store = SimpleNamespace(fork_after_turn=fork_after_turn)
    host.select = AsyncMock(side_effect=lambda o, s: order.append(f"select:{s}"))
    return host


class HostBranchAfterTurnTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_is_listed_and_does_not_still_the_source(self):
        """一轮收口处切新线：不顶掉来源，也不暂停、不退文件。"""
        order: list[str] = []
        host = host_watching(order)

        view = await host.branch_after_turn(
            "owner", "session-old", "user-1", "session-new"
        )

        self.assertEqual(view, "view")
        self.assertFalse(host._lineage.is_superseded("session-old"))
        self.assertFalse(host.is_paused("session-old"))
        self.assertFalse(host.is_paused("session-new"))
        self.assertEqual(
            order,
            [
                "fork_turn:user-1",
                "select:session-new",
                "view:session-new",
            ],
        )
