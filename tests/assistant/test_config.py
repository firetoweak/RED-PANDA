import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helperme.config import INITIAL_CONFIG, InitialConfigCreated, load_app_config
from helperme.llm.config import ModelConfig


class AppConfigTest(unittest.TestCase):
    def test_documented_model_examples_are_valid_json_configs(self):
        path = Path(__file__).resolve().parents[2] / "docs" / "模型配置.md"
        blocks = re.findall(
            r"```json\n(.*?)\n```",
            path.read_text(encoding="utf-8"),
            re.DOTALL,
        )
        examples = [
            value
            for block in blocks
            if set(value := json.loads(block)) == {"active", "router"}
        ]

        self.assertGreaterEqual(len(examples), 10)
        for example in examples:
            ModelConfig(**example)

    def _data(self, threshold: int = 200000) -> dict:
        return {
            "model": {
                "active": "model",
                "router": {
                    "model_list": [{
                        "model_name": "model",
                        "litellm_params": {
                            "model": "openai/provider-model",
                            "custom_field": {"kept": True},
                        },
                    }],
                    "num_retries": 0,
                },
            },
            "runtime": {
                "compact_threshold_tokens": threshold,
                "loop_guard_repeat_threshold": 3,
            },
            "channels": {},
        }

    def _write_config(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_example_config_matches_current_schema(self):
        path = Path(__file__).resolve().parents[2] / "config.example.json"

        config = load_app_config(path)
        document = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(document, INITIAL_CONFIG)
        self.assertIsNotNone(config.channels.telegram)

    def test_router_retry_defaults_are_injected_by_code(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["model"]["router"].pop("num_retries")
            self._write_config(path, data)

            config = load_app_config(path)

        self.assertEqual(config.model.router["num_retries"], 3)
        self.assertEqual(config.model.router["timeout"], 60)

    def test_loop_guard_threshold_is_validated_at_config_boundary(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in (0, 1, True, 3.5, "3"):
                with self.subTest(value=value):
                    data = self._data()
                    data["runtime"]["loop_guard_repeat_threshold"] = value
                    self._write_config(path, data)
                    with self.assertRaisesRegex(ValueError, "loop_guard_repeat_threshold"):
                        load_app_config(path)

    def test_first_run_creates_default_config_and_stops(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            expected_path = home / ".helperme" / "config.json"

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("helperme.paths.Path.home", return_value=home),
                self.assertRaises(InitialConfigCreated) as raised,
            ):
                load_app_config()

            document = json.loads(expected_path.read_text(encoding="utf-8"))

        self.assertEqual(raised.exception.path, expected_path.resolve())
        self.assertEqual(document, INITIAL_CONFIG)

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

        self.assertEqual(config.model.active, "model")
        self.assertEqual(
            config.model.router["model_list"][0]["litellm_params"]["custom_field"],
            {"kept": True},
        )
        self.assertEqual(config.runtime.compact_threshold_tokens, 200000)
        self.assertIsNone(config.channels.telegram)

    def test_parses_telegram_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["channels"] = {
                "telegram": {
                    "bot_token": " token ",
                    "allowed_chat_id": -7,
                }
            }
            self._write_config(path, data)

            telegram = load_app_config(path).channels.telegram

        self.assertIsNotNone(telegram)
        self.assertEqual(telegram.bot_token, "token")
        self.assertEqual(telegram.allowed_chat_id, -7)

    def test_allows_unpaired_telegram_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["channels"] = {
                "telegram": {
                    "bot_token": "token",
                    "allowed_chat_id": None,
                }
            }
            self._write_config(path, data)

            telegram = load_app_config(path).channels.telegram

        self.assertIsNotNone(telegram)
        self.assertIsNone(telegram.allowed_chat_id)

    def test_explicit_path_takes_priority_over_environment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self._write_config(path, self._data())

            with patch.dict(
                os.environ,
                {"HELPERME_CONFIG": str(path.with_name("missing.json"))},
            ):
                config = load_app_config(path)

        self.assertEqual(config.model.active, "model")

    def test_environment_overrides_default_path(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self._write_config(path, self._data())

            with patch.dict(os.environ, {"HELPERME_CONFIG": str(path)}):
                config = load_app_config()

        self.assertEqual(config.model.active, "model")

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

    def test_rejects_non_mapping_router(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["model"]["router"] = []
            self._write_config(path, data)

            with self.assertRaisesRegex(ValueError, "model.router"):
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

    def test_rejects_incomplete_telegram_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["channels"] = {"telegram": {"bot_token": "token"}}
            self._write_config(path, data)

            with self.assertRaisesRegex(ValueError, "allowed_chat_id"):
                load_app_config(path)

    def test_rejects_null_telegram_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["channels"] = {"telegram": None}
            self._write_config(path, data)

            with self.assertRaisesRegex(ValueError, "必须是映射"):
                load_app_config(path)
