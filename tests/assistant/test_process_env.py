from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helperme.assistant.host.process_env import (
    host_process_environment,
    install_host_environment,
    user_session_environment,
)


class HostProcessEnvironmentTest(unittest.TestCase):
    def test_keeps_current_path_prefix_and_appends_missing_session_dirs(self):
        session = {
            "PATH": os.pathsep.join(
                (
                    r"C:\Windows\System32\WindowsPowerShell\v1.0",
                    r"C:\Windows\system32",
                )
            ),
            "PATHEXT": ".COM;.EXE",
        }
        with patch(
            "helperme.assistant.host.process_env.user_session_environment",
            return_value=session,
        ):
            built = host_process_environment(
                {
                    "PATH": os.pathsep.join((r"C:\conda\env", r"C:\Windows\system32")),
                    "EXTRA_X": "1",
                }
            )

        parts = built["PATH"].split(os.pathsep)
        self.assertEqual(built["EXTRA_X"], "1")
        self.assertEqual(parts[0], r"C:\conda\env")
        self.assertIn(r"C:\Windows\System32\WindowsPowerShell\v1.0", parts)

    def test_fills_missing_identity_variable_from_user_session(self):
        with patch(
            "helperme.assistant.host.process_env.user_session_environment",
            return_value={"PATH": r"C:\Windows", "PATHEXT": ".EXE"},
        ):
            built = host_process_environment({"PATH": r"C:\Windows"})

        self.assertEqual(built["PATHEXT"], ".EXE")

    def test_leaves_environment_unchanged_when_no_session_block(self):
        current = {"PATH": "/usr/bin", "FOO": "bar"}
        with patch(
            "helperme.assistant.host.process_env.user_session_environment",
            return_value=None,
        ):
            built = host_process_environment(current)

        self.assertEqual(built, current)

    @unittest.skipUnless(os.name == "nt", "需要 Windows 用户会话环境")
    def test_install_preserves_program_lookup_with_mixed_case_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "rg.exe"
            executable.touch()
            with patch.dict(os.environ, {"PATH": directory, "HELPERME_TEST": "current"}):
                with patch(
                    "helperme.assistant.host.process_env.user_session_environment",
                    return_value={"Path": r"C:\Windows", "HelperMe_Test": "session"},
                ):
                    install_host_environment()

                self.assertEqual(Path(shutil.which("rg")), executable)
                self.assertEqual(os.environ["HELPERME_TEST"], "current")
                self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], directory)
                self.assertIn(r"C:\Windows", os.environ["PATH"].split(os.pathsep))

    @unittest.skipUnless(os.name == "nt", "需要 Windows 用户会话环境")
    def test_session_environment_contains_current_user_profile(self):
        session = {name.upper(): value for name, value in user_session_environment().items()}

        self.assertEqual(
            os.path.normcase(os.path.normpath(session["USERPROFILE"])),
            os.path.normcase(os.path.normpath(os.environ["USERPROFILE"])),
        )

    @unittest.skipUnless(os.name == "nt", "需要 Windows 用户会话环境")
    def test_stripped_path_can_discover_powershell_from_user_session(self):
        built = host_process_environment(
            {
                "PATH": r"C:\Windows\system32",
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            }
        )

        self.assertIsNotNone(shutil.which("powershell.exe", path=built["PATH"]))
