from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from redpanda.assistant.conversations import AssistantQueries, recent_workspace_id
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.workspaces import (
    SESSION_WORKSPACE_FACT,
    UnboundSessionError,
    bound_workspace,
    bound_workspace_id,
)
from redpanda.runtime import (
    DeliveryIdentity,
    DomainFactCommitted,
    EventDraft,
    SqliteJournal,
    UserMessageReceived,
)
from redpanda.sandbox.registry import WorkspaceRegistry


async def _append_user_message(store, session_id: str, text: str) -> None:
    await SqliteJournal(store.require(session_id)).accept_delivery(
        EventDraft(
            event_id=f"user-{text}",
            session_id=session_id,
            payload=UserMessageReceived(text),
            occurred_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
            delivery=DeliveryIdentity("web", f"delivery-{text}"),
        )
    )


async def _create_unbound(store, session_id: str, text: str) -> None:
    path = store.path(session_id)
    path.parent.mkdir()
    journal = SqliteJournal(path)
    await journal.create_session(session_id)
    await _append_user_message(store, session_id, text)


class _IdleSessions:
    def activity(self, session_id: str) -> str:
        return "idle"

    def is_superseded(self, session_id: str) -> bool:
        return False

    def is_archived(self, session_id: str) -> bool:
        return False

    def session_title(self, session_id: str) -> str | None:
        return None


class SessionWorkspaceBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_writes_the_binding_as_the_first_event(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))

            await store.create("session-1", workspace_id="workspace-a")

            events = await SqliteJournal(
                store.require("session-1")
            ).snapshot("session-1")
            self.assertEqual(bound_workspace_id(events), "workspace-a")
            seed = events[0].payload
            self.assertIsInstance(seed, DomainFactCommitted)
            self.assertEqual(seed.fact_type, SESSION_WORKSPACE_FACT)
            self.assertEqual(seed.data["workspace_id"], "workspace-a")

    async def test_bound_workspace_resolves_through_the_registry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            task_root = root / "task"
            task_root.mkdir()
            registry = WorkspaceRegistry.load(root / "workspaces.json")
            record = registry.create(name="demo", task_root=task_root)
            store = SessionStore(root / "sessions")
            await store.create("session-1", workspace_id=record.workspace_id)
            events = await SqliteJournal(
                store.require("session-1")
            ).snapshot("session-1")

            resolved = bound_workspace(
                "session-1",
                events,
                WorkspaceRegistry.load(root / "workspaces.json"),
            )

            self.assertEqual(resolved, record)

    async def test_unbound_session_is_an_explicit_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            await _create_unbound(store, "legacy", "存量会话")
            events = await SqliteJournal(
                store.require("legacy")
            ).snapshot("legacy")

            self.assertIsNone(bound_workspace_id(events))
            with self.assertRaises(UnboundSessionError):
                bound_workspace(
                    "legacy",
                    events,
                    WorkspaceRegistry.load(root / "workspaces.json"),
                )

    async def test_listing_hides_sessions_without_a_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            await store.create("bound", workspace_id="workspace-a")
            await _append_user_message(store, "bound", "有归属")
            await _create_unbound(store, "legacy", "无归属")

            listed = await AssistantQueries(store, _IdleSessions()).list_sessions()

            self.assertEqual(
                [summary.session_id for summary in listed],
                ["bound"],
            )
            self.assertEqual(listed[0].workspace_id, "workspace-a")
            self.assertEqual(recent_workspace_id(listed), "workspace-a")
            self.assertIsNone(recent_workspace_id(()))
