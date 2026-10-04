from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.assistant.session_metadata import SessionFlagStore, SessionLineageStore
from redpanda.assistant.workspace_versions import (
    WORKSPACE_VERSION_FACT,
    StepNotRewindable,
)
from redpanda.runtime.events import DomainFactCommitted


def version_event(step_id, version):
    return SimpleNamespace(
        event_id=f"fact-{step_id}",
        payload=DomainFactCommitted(
            WORKSPACE_VERSION_FACT,
            {
                "workspace_id": "workspace-1",
                "step_id": step_id,
                "version": version,
                "error": None if version else "snapshot failed",
            },
        ),
    )


def host_watching(order):
    host = object.__new__(HostSupervisor)
    host.models = None
    host._pause = SessionFlagStore(None, "paused.json")
    host._lineage = SessionLineageStore(None, "lineage.json")
    host._with_host_metadata = lambda observed, session_id: observed
    host.locks = {}

    async def application(operation, session_id, arguments):
        order.append(f"{operation}:{session_id}")
        return "view"

    async def fork_after_event(source, boundary_event_id, child):
        order.append(f"fork:{boundary_event_id}")

    host.compact = SimpleNamespace(application=AsyncMock(side_effect=application))
    host.store = SimpleNamespace(
        require=lambda session_id: "journal",
        fork_after_event=fork_after_event,
    )
    host.wait_quiescent = AsyncMock(side_effect=lambda s: order.append("quiescent"))
    host.select = AsyncMock(side_effect=lambda o, s: order.append(f"select:{s}"))
    return host


def journal_of(*events):
    return patch(
        "redpanda.assistant.host.supervisor.SqliteJournal",
        lambda path: SimpleNamespace(snapshot=AsyncMock(return_value=events)),
    )


class HostRestartFromStepTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_source_is_stilled_before_a_new_identity_takes_over(self):
        """先停源再切，切完新身份顶掉旧的并且带着暂停。

        源还在跑就切，跑着的命令会往共享工作树里写、也会继续追加事件；
        新身份不暂停就会自己往下走，而人要的是停在那一刻换条走法。
        """
        order: list[str] = []
        host = host_watching(order)

        with journal_of(version_event("step-1", "a" * 40)):
            view = await host.restart_from_step(
                "owner", "session-old", "step-1", "session-new", "web-1"
            )

        self.assertEqual(view, "view")
        self.assertTrue(host.is_paused("session-old"))
        self.assertTrue(host.is_paused("session-new"))
        self.assertTrue(host._lineage.is_superseded("session-old"))
        self.assertEqual(
            order,
            [
                "cancel_turn:session-old",
                "quiescent",
                "fork:fact-step-1",
                "select:session-new",
                "settle_forked_workspace:session-new",
                "view:session-new",
            ],
        )

    async def test_a_step_without_a_version_is_refused_before_anything_moves(self):
        order: list[str] = []
        host = host_watching(order)

        with journal_of(version_event("step-1", None)):
            with self.assertRaises(StepNotRewindable):
                await host.restart_from_step(
                    "owner", "session-old", "step-1", "session-new", "web-1"
                )

        self.assertEqual(order, [])
        self.assertFalse(host.is_paused("session-old"))
