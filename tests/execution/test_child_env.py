import os
import unittest
from unittest.mock import MagicMock, patch

from konden.execution.command import (
    BoundedTextCapture,
    CaptureLimit,
    strip_terminal_control,
)
from konden.execution.local.child_env import latest_persistent_path
from konden.execution.local.powershell import CommandEnvironmentPolicy


class ChildEnvOverlayTest(unittest.TestCase):
    def test_overlay_is_applied_to_child_environment(self):
        env = CommandEnvironmentPolicy().build({})
        for name, value in {
            "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat",
            "GH_PAGER": "cat", "CI": "1",
        }.items():
            self.assertEqual(env[name], value)

    def test_path_is_replaced_with_latest_persistent_value(self):
        with patch(
            "konden.execution.local.powershell.latest_persistent_path",
            return_value="C:\\new;C:\\tools",
        ):
            env = CommandEnvironmentPolicy().build(
                {"PATH": "C:\\old", "SYSTEMROOT": "C:\\Windows"}
            )
        self.assertEqual(env["PATH"], "C:\\new;C:\\tools")

    def test_path_falls_back_to_host_snapshot_without_persistent_value(self):
        with patch(
            "konden.execution.local.powershell.latest_persistent_path",
            return_value=None,
        ):
            env = CommandEnvironmentPolicy().build({"PATH": "C:\\old"})
        self.assertEqual(env["PATH"], "C:\\old")

    def test_path_is_added_when_host_snapshot_lacks_it(self):
        with patch(
            "konden.execution.local.powershell.latest_persistent_path",
            return_value="C:\\new",
        ):
            env = CommandEnvironmentPolicy().build({"SYSTEMROOT": "C:\\Windows"})
        self.assertEqual(env["PATH"], "C:\\new")

    def test_explicit_path_override_wins_without_duplicate_windows_names(self):
        with patch(
            "konden.execution.local.powershell.latest_persistent_path",
            return_value="system-tools",
        ):
            env = CommandEnvironmentPolicy(fixed_values={"PATH": "work-tools"}).build({"Path": "host-tools"})

        self.assertEqual(env["PATH"], "work-tools")
        self.assertNotIn("Path", env)
        self.assertNotIn("VIRTUAL_ENV", env)

    def test_workspace_policy_preserves_activation_without_reading_registry(self):
        source = {"Path": "project-tools;system-tools", "CONDA_PREFIX": "project-env"}
        with patch("konden.execution.local.powershell.latest_persistent_path") as refresh:
            env = CommandEnvironmentPolicy(
                forward_names=tuple(source), refresh_path=False,
            ).build(source)

        refresh.assert_not_called()
        self.assertEqual(env["Path"], source["Path"])
        self.assertEqual(env["CONDA_PREFIX"], "project-env")

    @unittest.skipUnless(os.name == "nt", "Windows 注册表读取")
    def test_combines_machine_and_expanded_user_registry_paths(self):
        import winreg

        machine, user = MagicMock(), MagicMock()
        with (
            patch("winreg.OpenKey", side_effect=[machine, user]) as opened,
            patch("winreg.QueryValueEx", side_effect=[
                (r"C:\system-tools", winreg.REG_SZ),
                (r"%KONDEN_TEST_TOOLS%\bin", winreg.REG_EXPAND_SZ),
            ]) as queried,
            patch.dict(os.environ, {"KONDEN_TEST_TOOLS": r"C:\user-tools"}),
        ):
            self.assertEqual(
                latest_persistent_path(), r"C:\system-tools;C:\user-tools\bin",
            )

        self.assertEqual(opened.call_args_list[0].args, (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ))
        self.assertEqual(opened.call_args_list[1].args, (
            winreg.HKEY_CURRENT_USER, "Environment",
        ))
        self.assertEqual([call.args for call in queried.call_args_list], [
            (machine.__enter__.return_value, "Path"),
            (user.__enter__.return_value, "Path"),
        ])


class StripTerminalControlTest(unittest.TestCase):
    def test_strips_csi_color(self):
        self.assertEqual(strip_terminal_control("\x1b[31mred\x1b[0m"), "red")

    def test_strips_osc_title_with_bel_or_st(self):
        self.assertEqual(strip_terminal_control("\x1b]0;title\x07body"), "body")
        self.assertEqual(strip_terminal_control("\x1b]0;title\x1b\\body"), "body")

    def test_strips_stray_escape_and_control_chars(self):
        self.assertEqual(strip_terminal_control("a\x1bXb\x07c\x00d"), "abcd")

    def test_keeps_tab_newline_and_cr(self):
        self.assertEqual(strip_terminal_control("a\tb\nc\rd"), "a\tb\nc\rd")

    def test_plain_text_unchanged(self):
        self.assertEqual(strip_terminal_control("hello 世界"), "hello 世界")

    def test_capture_feed_sanitizes_and_counts_stripped_chars(self):
        capture = BoundedTextCapture(CaptureLimit(max_chars=100, head_chars=50))
        capture.feed("\x1b[31mabc\x1b[0m")
        output = capture.finish()
        self.assertEqual(output.content, "abc")
        self.assertEqual(output.total_chars, 3)
