from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from konden.assistant.host.session_store import SessionStore
from konden.runtime import AgentRuntime, SqliteJournal, StateProjector


class SessionStoreListingTest(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_unchanged_projection_but_observes_independent_writes_and_isolates_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            first = SessionStore(Path(directory) / "first")
            second = SessionStore(Path(directory) / "second")
            for store in (first, second):
                await store.create("same-id")
            writer = AgentRuntime(SqliteJournal(first.require("same-id")), None, {})
            event = await writer.receive_user_message("same-id", "first", delivery_id="first")

            with patch.object(StateProjector, "project", autospec=True,
                              side_effect=StateProjector.project) as project:
                events, state = await first.projected("same-id")
                self.assertEqual(state.next_trigger_event_ids, (event.event_id,))
                self.assertEqual(await first.projected("same-id"), (events, state))
                self.assertEqual(project.call_count, 1)

                other_events, other_state = await second.projected("same-id")
                self.assertEqual(other_events, ())
                self.assertEqual(other_state.next_trigger_event_ids, ())

                new = await writer.receive_user_message("same-id", "new", delivery_id="new")
                updated_events, updated = await first.projected("same-id")
                self.assertEqual(updated_events, (*events, new))
                self.assertEqual(updated.next_trigger_event_ids, (event.event_id, new.event_id))
                self.assertEqual(project.call_count, 3)

    @unittest.skipUnless(os.name == "nt", "Windows directory rename sharing contract")
    async def test_creation_retries_a_transient_windows_reader_without_losing_initial_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            rename = os.rename
            blocked = False

            def briefly_read(source, target):
                nonlocal blocked
                if not blocked:
                    blocked = True
                    with (source / "agent-creation.json").open("rb"):
                        return rename(source, target)
                return rename(source, target)

            with patch("konden.assistant.host.session_store.os.rename", side_effect=briefly_read):
                await store.create("created", workspace_id="workspace-1", agent_colleagues=("peer",))

            journal = SqliteJournal(store.require("created"))
            self.assertEqual(await journal.session_identity(), "created")
            self.assertEqual(len(await journal.snapshot("created")), 1)
            self.assertEqual(json.loads((store.path("created").parent / "agent-creation.json").read_text()),
                             {"colleagues": ["peer"]})
            self.assertEqual(list(store.root.glob(".creating-*")), [])

    @unittest.skipUnless(os.name == "nt", "Windows directory rename sharing contract")
    async def test_creation_preserves_permanent_rename_errors_and_bounds_sharing_retries(self):
        for winerror in (5, 1314):
            with self.subTest(winerror=winerror), tempfile.TemporaryDirectory() as directory:
                store = SessionStore(Path(directory))
                error = PermissionError(errno.EACCES, "rename denied", None, winerror)
                with (patch("konden.assistant.host.session_store.os.rename", side_effect=error) as rename,
                      patch("konden.assistant.host.session_store.asyncio.sleep", new_callable=AsyncMock),
                      self.assertRaises(PermissionError) as caught):
                    await store.create("denied")
                self.assertIs(caught.exception, error)
                if winerror == 5:
                    self.assertGreater(rename.call_count, 1)
                else:
                    rename.assert_called_once()
                self.assertEqual(store.journals(), ())

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
