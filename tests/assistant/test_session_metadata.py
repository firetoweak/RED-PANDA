from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from helperme.assistant.session_metadata import (
    SessionFlagStore,
    SessionLineageStore,
    SessionTextStore,
)


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


class SessionTextStoreTest(unittest.TestCase):
    def test_missing_key_is_none_and_only_explicit_text_is_written(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionTextStore(root, "titles.json")

            self.assertIsNone(store.get("session-1"))
            self.assertFalse((root / "titles.json").is_file())

            store.set("session-1", "  我起的名  ")
            raw = json.loads((root / "titles.json").read_text(encoding="utf-8"))
            self.assertEqual(raw, {"session-1": "我起的名"})
            self.assertEqual(
                SessionTextStore(root, "titles.json").get("session-1"),
                "我起的名",
            )
            self.assertEqual(store.items(), {"session-1": "我起的名"})


class SessionLineageStoreTest(unittest.TestCase):
    def test_only_the_head_of_a_line_is_not_superseded(self):
        """连续改写只留下最后一个身份代表这条线，中间的都被顶掉了。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionLineageStore(root, "lineage.json")

            store.supersede("child", "parent")
            store.supersede("grandchild", "child")

            reloaded = SessionLineageStore(root, "lineage.json")
            self.assertTrue(reloaded.is_superseded("parent"))
            self.assertTrue(reloaded.is_superseded("child"))
            self.assertFalse(reloaded.is_superseded("grandchild"))
            self.assertFalse(reloaded.is_superseded("unrelated"))
