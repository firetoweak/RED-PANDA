import os
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
