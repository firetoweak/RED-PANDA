import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from helperme.config import write_json
from helperme.model_settings import ModelSettings, ModelConfigurationError, ModelInUseError
from helperme.paths import HelperMeHome

PRO = {"model": "deepseek/pro", "compact_threshold_tokens": 200000}
FLASH = {"model": "deepseek/flash", "compact_threshold_tokens": 64000}


class ModelSettingsTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.home = HelperMeHome(Path(self.directory.name))
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
