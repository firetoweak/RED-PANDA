import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redpanda.paths import RedPandaHome


class RedPandaHomeTest(unittest.TestCase):
    def test_default_root_is_hidden_redpanda_in_user_home(self):
        with patch("redpanda.paths.Path.home", return_value=Path("C:/Users/test")):
            home = RedPandaHome.default()

        self.assertEqual(home.root, Path("C:/Users/test/.redpanda").resolve())

    def test_environment_overrides_default_root(self):
        with patch.dict(os.environ, {"REDPANDA_HOME": "C:/instances/agent"}):
            home = RedPandaHome.default()

        self.assertEqual(home.root, Path("C:/instances/agent").resolve())

    def test_layout_contains_product_data_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            home = RedPandaHome(Path(directory) / ".redpanda")

            home.initialize()

            self.assertTrue(home.sessions_root.is_dir())
            self.assertTrue(home.mcp_root.is_dir())
            self.assertTrue(home.skills_root.is_dir())
            self.assertTrue(home.state_root.is_dir())
            self.assertEqual(home.config_path, home.root / "config.json")
            self.assertEqual(home.sessions_root.parent, home.root)
            self.assertEqual(home.mcp_root.parent, home.root)
            self.assertEqual(home.skills_root.parent, home.root)
            self.assertEqual(home.state_root.parent, home.root)
            self.assertEqual(home.runtime_sessions_root.parent, home.root)
