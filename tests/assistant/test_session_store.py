from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from redpanda.assistant.host.session_store import SessionStore
from redpanda.runtime import SqliteJournal


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
