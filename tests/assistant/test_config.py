import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helperme.config import INITIAL_CONFIG, load_app_config
from helperme.llm.config import ModelConfig


class AppConfigTest(unittest.TestCase):
    def _data(self, threshold: int = 200000) -> dict:
        return {
            "model": {
                "active": "deepseek/model",
            },
            "runtime": {
                "compact_threshold_tokens": threshold,
            },
        }

    def _write_config(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_keeps_selected_model(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            self._write_config(path, data)

            config = load_app_config(path)

        self.assertEqual(config.model.active, "deepseek/model")

    def test_first_run_writes_usable_defaults_and_continues(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            expected_path = home / ".helperme" / "config.json"

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("helperme.paths.Path.home", return_value=home),
            ):
                config = load_app_config()

            document = json.loads(expected_path.read_text(encoding="utf-8"))

        self.assertEqual(document, INITIAL_CONFIG)
        self.assertEqual(config.model.active, INITIAL_CONFIG["model"]["active"])
        self.assertEqual(
            config.runtime.compact_threshold_tokens,
            INITIAL_CONFIG["runtime"]["compact_threshold_tokens"],
        )

    def test_loads_default_config_from_helperme_home(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            config_path = home / ".helperme" / "config.json"
            config_path.parent.mkdir()
            self._write_config(config_path, self._data())

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("helperme.paths.Path.home", return_value=home),
            ):
                config = load_app_config()

        self.assertEqual(config.model.active, "deepseek/model")
        self.assertEqual(config.runtime.compact_threshold_tokens, 200000)

    def test_explicit_path_takes_priority_over_environment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self._write_config(path, self._data())

            with patch.dict(
                os.environ,
                {"HELPERME_CONFIG": str(path.with_name("missing.json"))},
            ):
                config = load_app_config(path)

        self.assertEqual(config.model.active, "deepseek/model")

    def test_environment_overrides_default_path(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self._write_config(path, self._data())

            with patch.dict(os.environ, {"HELPERME_CONFIG": str(path)}):
                config = load_app_config()

        self.assertEqual(config.model.active, "deepseek/model")

    def test_explicit_missing_path_is_not_created(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "custom" / "config.json"

            with self.assertRaises(FileNotFoundError):
                load_app_config(path)

            self.assertFalse(path.exists())

    def test_rejects_unknown_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["runtime"]["unexpected"] = 8
            self._write_config(path, data)

            with self.assertRaises(ValueError):
                load_app_config(path)

    def test_rejects_model_without_a_built_in_provider(self):
        for active in ("deepseek-v4-pro", "openai/gpt", "deepseek/"):
            with self.subTest(active=active):
                with self.assertRaisesRegex(ValueError, "<provider>/<model>"):
                    ModelConfig(active=active)

    def test_rejects_gateway_and_model_parameter_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["model"]["gateway"] = {"base_url": "http://localhost/v1"}
            data["model"]["request_options"] = {"temperature": 0}
            self._write_config(path, data)

            with self.assertRaisesRegex(ValueError, "配置字段必须只有 active"):
                load_app_config(path)

    def test_compact_threshold_is_required_and_a_positive_integer(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in (None, 0, -1, True, 0.55, "200000"):
                with self.subTest(value=value):
                    data = self._data(value)
                    if value is None:
                        del data["runtime"]["compact_threshold_tokens"]
                    self._write_config(path, data)
                    with self.assertRaisesRegex(ValueError, "compact_threshold_tokens"):
                        load_app_config(path)
            data = self._data(123456)
            self._write_config(path, data)
            self.assertEqual(load_app_config(path).runtime.compact_threshold_tokens, 123456)
