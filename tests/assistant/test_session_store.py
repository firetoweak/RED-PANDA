from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.toolsets import (
    LOAD_TOOLSET,
    ToolSurface,
    load_toolset_binding,
)
from helperme.runtime import AgentRuntime, InvokeTool, ModelDecision, SqliteJournal
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.assistant.test_toolsets import FakeEchoProvider, _schema_names
from tests.session_scheduler import settle_session


class SessionStoreListingTest(unittest.IsolatedAsyncioTestCase):
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
