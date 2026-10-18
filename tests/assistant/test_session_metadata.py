from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from redpanda.assistant.session_metadata import SessionFlagStore


class SessionFlagStoreTest(unittest.TestCase):
    def test_missing_key_is_false_and_only_explicit_toggle_is_written(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionFlagStore(root, "paused.json")

            self.assertFalse(store.get("session-1"))
            self.assertFalse((root / "paused.json").is_file())

            store.remember("session-1", False)
            self.assertFalse((root / "paused.json").is_file())

            store.set("session-1", True)
            raw = json.loads((root / "paused.json").read_text(encoding="utf-8"))
            self.assertEqual(raw, {"session-1": True})
            self.assertTrue(SessionFlagStore(root, "paused.json").get("session-1"))

    def test_each_file_is_an_independent_flag(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            SessionFlagStore(root, "paused.json").set("session-1", True)

            self.assertFalse(SessionFlagStore(root, "lineage.json").get("session-1"))
