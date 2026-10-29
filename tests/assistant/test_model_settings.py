import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from konden.config import write_json
from konden.model_settings import ModelSettings, ModelConfigurationError, ModelInUseError
from konden.paths import KondenHome

PRO = {"model": "deepseek/pro", "compact_threshold_tokens": 200000}
FLASH = {"model": "deepseek/flash", "compact_threshold_tokens": 64000}


class ModelSettingsTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.home = KondenHome(Path(self.directory.name))
        write_json(self.home.config_path, {"model": {"default": PRO["model"], "candidates": [PRO, FLASH]}})
        self.models = ModelSettings(self.home, self.home.runtime_sessions_root, path=self.home.config_path)
        connections = json.loads(self.home.connections_path.read_text(encoding="utf-8"))
        connections["deepseek"]["api_key"] = "test"
        write_json(self.home.connections_path, connections)

    def tearDown(self):
        self.directory.cleanup()

    def test_default_change_affects_only_new_sessions_and_selection_survives_reload(self):
        self.models.initialize_session("old")
        self.models.save({"model": {"default": FLASH["model"], "candidates": [PRO, FLASH]}})
        self.models.initialize_session("new")
        self.models.initialize_session("branch", parent="old")
        reloaded = ModelSettings(self.home, self.home.runtime_sessions_root, path=self.home.config_path)
        self.assertEqual(reloaded.selected("old"), PRO["model"])
        self.assertEqual(reloaded.selected("branch"), PRO["model"])
        self.assertEqual(reloaded.selected("new"), FLASH["model"])

    def test_retrying_initialization_after_a_write_failure_persists_once_and_preserves_selection(self):
        with (patch("konden.model_settings.write_json", side_effect=PermissionError("locked")),
              self.assertRaises(PermissionError)):
            self.models.initialize_session("recovering")
        self.models.initialize_session("recovering")
        self.models.select("recovering", FLASH["model"])
        self.models.initialize_session("recovering")
        reloaded = ModelSettings(self.home, self.home.runtime_sessions_root, path=self.home.config_path)
        self.assertEqual(reloaded.selected("recovering"), FLASH["model"])

    def test_selection_applies_at_next_decision_with_its_threshold(self):
        self.models.initialize_session("session")
        captured = self.models.for_decision("session")
        selection = self.models.select("session", FLASH["model"])
        self.assertEqual(captured, PRO)
        self.assertEqual(selection, {"selected": FLASH, "effective": PRO, "pending": True})
        self.assertEqual(self.models.for_decision("session"), FLASH)
        self.assertFalse(self.models.selection("session")["pending"])

    def test_default_and_session_references_prevent_deletion(self):
        self.models.initialize_session("session")
        with self.assertRaises(ModelInUseError):
            self.models.save({"model": {"default": FLASH["model"], "candidates": [FLASH]}})
        self.models.save({"model": {"default": FLASH["model"], "candidates": [PRO, FLASH]}})
        with self.assertRaises(ModelInUseError):
            self.models.save({"model": {"default": FLASH["model"], "candidates": [FLASH]}})
        self.models.select("session", FLASH["model"])
        self.models.save({"model": {"default": FLASH["model"], "candidates": [FLASH]}})

    def test_new_default_and_removing_the_unused_old_default_are_saved_together(self):
        self.models.save({"model": {"default": FLASH["model"], "candidates": [FLASH]}})
        self.assertEqual(self.models.config().default_model, FLASH["model"])
        self.assertEqual([item.model for item in self.models.config().models], [FLASH["model"]])

    def test_session_reasoning_effort_is_independent_of_later_default_edits(self):
        connections = json.loads(self.home.connections_path.read_text(encoding="utf-8"))
        connections["stepfun"]["api_key"] = "test"
        write_json(self.home.connections_path, connections)
        step5 = {"model": "stepfun/step-5-preview", "compact_threshold_tokens": 200000,
                 "reasoning_effort": "high"}
        self.models.save({"model": {"default": step5["model"], "candidates": [PRO, FLASH, step5]}})
        self.models.initialize_session("step5")
        captured = self.models.for_decision("step5")
        updated = {**step5, "reasoning_effort": "low"}
        self.models.save({"model": {"default": step5["model"], "candidates": [PRO, FLASH, updated]}})
        self.assertEqual(captured, step5)
        self.assertEqual(self.models.selection("step5"), {
            "selected": step5, "effective": step5, "pending": False,
        })
        self.models.select("step5", step5["model"], "low")
        self.assertEqual(self.models.for_decision("step5"), updated)
        self.assertFalse(self.models.selection("step5")["pending"])

    def test_failed_selection_write_does_not_publish_an_uncommitted_choice(self):
        self.models.initialize_session("session")
        with patch("konden.model_settings.write_json", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.models.select("session", FLASH["model"], "max")
        self.assertEqual(self.models.selected("session"), PRO["model"])
        reloaded = ModelSettings(self.home, self.home.runtime_sessions_root, path=self.home.config_path)
        self.assertEqual(reloaded.selection("session")["selected"], PRO)

    def test_existing_model_names_upgrade_once_to_complete_selections(self):
        target = self.home.runtime_sessions_root / "models.json"
        write_json(target, {"old": PRO["model"]})
        upgraded = ModelSettings(self.home, self.home.runtime_sessions_root, path=self.home.config_path)
        self.assertEqual(upgraded.selected("old"), PRO["model"])
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {
            "old": {"model": PRO["model"], "reasoning_effort": None}})
        write_json(target, {"": PRO["model"]})
        damaged = target.read_bytes()
        with self.assertRaises(ValueError):
            ModelSettings(self.home, self.home.runtime_sessions_root, path=self.home.config_path)
        self.assertEqual(target.read_bytes(), damaged)

    def test_unconfigured_candidate_is_rejected_before_selection_is_written(self):
        self.models.initialize_session("session")
        connections = json.loads(self.home.connections_path.read_text(encoding="utf-8"))
        connections["deepseek"]["api_key"] = ""
        write_json(self.home.connections_path, connections)
        with self.assertRaises(ModelConfigurationError):
            self.models.select("session", FLASH["model"])
        self.assertEqual(self.models.selected("session"), PRO["model"])

    def test_corrupt_persisted_config_does_not_become_a_client_input_error(self):
        self.models.initialize_session("session")
        self.home.config_path.write_text("{", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.models.select("session", FLASH["model"])

    def test_no_candidates_still_allows_initializing_an_idle_session(self):
        write_json(self.home.config_path, {"model": {"default": None, "candidates": []}})
        self.models.initialize_session("empty")
        self.assertIsNone(self.models.for_decision("empty", apply=False))
        with self.assertRaises(ModelConfigurationError):
            self.models.require_ready("empty")
