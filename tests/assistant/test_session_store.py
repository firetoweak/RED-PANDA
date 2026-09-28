from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from helperme.assistant.artifacts import FileArtifactGateway
from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.toolsets import (
    LOAD_TOOLSET,
    ToolSurface,
    load_toolset_binding,
)
from helperme.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal, replay
from helperme.runtime.events import DeliveryIdentity, DomainFactCommitted, EventDraft
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.assistant.test_toolsets import FakeEchoProvider, _schema_names
from tests.session_scheduler import settle_session


class SessionStoreListingTest(unittest.IsolatedAsyncioTestCase):
    async def test_nested_branch_reads_artifact_without_copying_its_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            await store.create("A", workspace_id="workspace-1")
            artifacts = FileArtifactGateway(store.root)
            ref = artifacts.for_session("A").save("original result")
            a = SqliteJournal(store.require("A"))
            event = (await a.accept_delivery(EventDraft(
                event_id="artifact-fact", session_id="A",
                payload=DomainFactCommitted(
                    "test.artifact", {"artifact": ref.artifact_id}, False
                ),
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity("test", "artifact"),
            ))).event
            await store.fork_after_event("A", event.event_id, "B")
            branch_ref = artifacts.for_session("B").save("branch result")
            branch_event = (await SqliteJournal(store.require("B")).accept_delivery(
                EventDraft(
                    event_id="branch-artifact-fact", session_id="B",
                    payload=DomainFactCommitted(
                        "test.artifact", {"artifact": branch_ref.artifact_id}, False
                    ),
                    occurred_at=datetime.now(timezone.utc),
                    delivery=DeliveryIdentity("test", "branch-artifact"),
                )
            )).event
            await store.fork_after_event("B", branch_event.event_id, "C")
            self.assertFalse((store.path("C").parent / "artifacts").exists())
            self.assertEqual(
                artifacts.for_session("C").read(ref.artifact_id, 0, 100).content,
                "original result",
            )
            self.assertEqual(
                artifacts.for_session("C").read(branch_ref.artifact_id, 0, 100).content,
                "branch result",
            )

    async def test_branch_consumes_an_inherited_decision_trigger_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            await store.create("A", workspace_id="workspace-1")
            source = AgentRuntime(
                SqliteJournal(store.require("A")),
                ScriptedDecisionMaker((lambda _: ModelDecision(content="first"),)),
                {},
            )
            await source.receive_user_message("A", "first", delivery_id="user")
            await source.advance("A")
            trigger = await source.receive_domain_fact(
                "A", "test.trigger", {}, delivery_id="trigger",
                source="test", requests_decision=True,
            )
            await store.fork_after_event("A", trigger.event_id, "B")
            child = AgentRuntime(
                SqliteJournal(store.require("B")),
                ScriptedDecisionMaker((lambda _: ModelDecision(content="child"),)),
                {},
            )
            self.assertIsNotNone((await child.advance("B")).step)
            self.assertEqual(
                replay("B", await child.snapshot("B")).state.waiting_for,
                ("external_fact",),
            )
            self.assertEqual(len(await source.snapshot("A")), 4)

    async def test_inherited_delivery_stays_idempotent_on_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            await store.create("A", workspace_id="workspace-1")
            a = SqliteJournal(store.require("A"))
            payload = DomainFactCommitted("test.fact", {"value": 1}, False)
            original = (await a.accept_delivery(EventDraft(
                event_id="fact-a", session_id="A", payload=payload,
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity("test", "same"),
            ))).event
            await store.fork_after_event("A", original.event_id, "B")
            b = SqliteJournal(store.require("B"))
            repeated = await b.accept_delivery(EventDraft(
                event_id="fact-b", session_id="B", payload=payload,
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity("test", "same"),
            ))
            self.assertFalse(repeated.inserted)
            self.assertEqual(repeated.event.event_id, original.event_id)
            self.assertEqual(len(await b.snapshot("B")), 2)

    async def test_nested_branches_store_only_local_events_and_freeze_ancestor_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            await store.create("A", workspace_id="workspace-1")
            a = SqliteJournal(store.require("A"))

            async def fact(journal, session_id, name):
                return (await journal.accept_delivery(EventDraft(
                    event_id=name,
                    session_id=session_id,
                    payload=DomainFactCommitted("test.fact", {"name": name}, False),
                    occurred_at=datetime.now(timezone.utc),
                    delivery=DeliveryIdentity("test", name),
                ))).event

            second = await fact(a, "A", "a-2")
            third = await fact(a, "A", "a-3")
            await store.fork_after_event("A", third.event_id, "B")
            b = SqliteJournal(store.require("B"))
            fourth = await fact(b, "B", "b-4")
            await store.fork_after_event("B", fourth.event_id, "C")
            await store.fork_after_event("B", second.event_id, "B-rewound")
            await fact(a, "A", "a-4")

            c = SqliteJournal(store.require("C"))
            self.assertEqual(
                [event.event_id for event in await c.snapshot("C")],
                [event.event_id for event in (await a.snapshot("A"))[:3]] + ["b-4"],
            )
            self.assertEqual(
                [event.event_id for event in await SqliteJournal(
                    store.require("B-rewound")
                ).snapshot("B-rewound")],
                [event.event_id for event in (await a.snapshot("A"))[:2]],
            )
            self.assertTrue(all(event.session_id == "C" for event in await c.snapshot("C")))
            with closing(sqlite3.connect(store.require("C"))) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM events WHERE inherited = 0"
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM history_spans"
                ).fetchone()[0], 2)
            await fact(c, "C", "c-5")
            self.assertEqual(len(await c.snapshot("C")), 5)
            self.assertEqual(len(await b.snapshot("B")), 4)

    async def test_lists_identity_hidden_by_hashed_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            await store.create("session-visible", workspace_id="workspace-1")
            (Path(directory) / "conversations.sqlite").touch()
            (Path(directory) / "_backup_session-visible").mkdir()

            journals = store.journals()

            self.assertEqual(len(journals), 1)
            self.assertEqual(
                await SqliteJournal(journals[0]).session_identity(),
                "session-visible",
            )

    async def test_fork_inherits_complete_prefix_and_loaded_toolsets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            await store.create("source", workspace_id="workspace-1")
            journal = SqliteJournal(store.require("source"))
            surface = ToolSurface(providers=(FakeEchoProvider(),))
            runtime = AgentRuntime(
                journal,
                ScriptedDecisionMaker(
                    (
                        lambda _frame: ModelDecision(
                            content="loading",
                            command_requests=(
                                InvokeTool(
                                    LOAD_TOOLSET,
                                    (("toolset_id", "demo"),),
                                ),
                            ),
                        ),
                        lambda _frame: ModelDecision(
                            content="done",
                            command_requests=(
                                InvokeTool(
                                    DELIVER_TOOL_NAME,
                                    (("output_id", "first"), ("text", "done")),
                                ),
                            ),
                        ),
                    )
                ),
                {
                    **load_toolset_binding(surface),
                    **deliver_binding(lambda *_args: None),
                },
                SequentialIds(),
            )
            surface.attach(runtime)
            surface.apply_catalog("source", surface.registry_descriptors())
            await runtime.receive_user_message(
                "source", "first", delivery_id="first"
            )
            await settle_session(runtime, "source")
            edited = await runtime.receive_user_message(
                "source", "old text", delivery_id="second"
            )

            original = await store.fork_before_message(
                "source", edited.event_id, "child"
            )

            self.assertEqual(original.content, "old text")
            source_events = await journal.snapshot("source")
            child_events = await SqliteJournal(store.require("child")).snapshot(
                "child"
            )
            self.assertEqual(
                [event.event_id for event in child_events],
                [event.event_id for event in source_events[:-1]],
            )
            self.assertTrue(all(event.session_id == "child" for event in child_events))
            restored = ToolSurface(providers=(FakeEchoProvider(),))
            child_runtime = AgentRuntime(
                SqliteJournal(store.require("child")),
                ScriptedDecisionMaker(
                    (
                        lambda _frame: ModelDecision(
                            content="edited",
                            command_requests=(
                                InvokeTool(
                                    DELIVER_TOOL_NAME,
                                    (("output_id", "edited"), ("text", "edited")),
                                ),
                            ),
                        ),
                    )
                ),
                deliver_binding(lambda *_args: None),
            )
            restored.attach(child_runtime)
            restored.apply_catalog("child", restored.registry_descriptors())
            await restored.rehydrate("child", child_events)
            self.assertIn("demo_ping", _schema_names(restored.schemas("child")))
            await child_runtime.receive_user_message(
                "child", "new text", delivery_id="edited"
            )
            await settle_session(child_runtime, "child")
            self.assertEqual(len(await journal.snapshot("source")), len(source_events))

    async def test_fork_after_turn_keeps_this_turn_and_drops_the_next_user_message(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            await store.create("source", workspace_id="workspace-1")
            journal = SqliteJournal(store.require("source"))
            surface = ToolSurface(providers=(FakeEchoProvider(),))
            runtime = AgentRuntime(
                journal,
                ScriptedDecisionMaker(
                    (
                        lambda _frame: ModelDecision(
                            content="done",
                            command_requests=(
                                InvokeTool(
                                    DELIVER_TOOL_NAME,
                                    (("output_id", "first"), ("text", "done")),
                                ),
                            ),
                        ),
                    )
                ),
                deliver_binding(lambda *_args: None),
                SequentialIds(),
            )
            surface.attach(runtime)
            first = await runtime.receive_user_message(
                "source", "first", delivery_id="first"
            )
            await settle_session(runtime, "source")
            await runtime.receive_user_message(
                "source", "later", delivery_id="second"
            )

            await store.fork_after_turn("source", first.event_id, "child")

            source_events = await journal.snapshot("source")
            child_events = await SqliteJournal(store.require("child")).snapshot(
                "child"
            )
            self.assertEqual(
                [event.event_id for event in child_events],
                [event.event_id for event in source_events[:-1]],
            )
            self.assertTrue(all(event.session_id == "child" for event in child_events))
