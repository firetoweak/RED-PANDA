import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from konden.paths import KondenHome


class KondenHomeTest(unittest.TestCase):
    def test_default_root_is_hidden_konden_in_user_home(self):
        with patch("konden.paths.Path.home", return_value=Path("C:/Users/test")):
            home = KondenHome.default()

        self.assertEqual(home.root, Path("C:/Users/test/.konden").resolve())

    def test_environment_overrides_default_root(self):
        with patch.dict(os.environ, {"KONDEN_HOME": "C:/instances/agent"}):
            home = KondenHome.default()

        self.assertEqual(home.root, Path("C:/instances/agent").resolve())

    def test_layout_contains_product_data_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            home = KondenHome(Path(directory) / ".konden")

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
