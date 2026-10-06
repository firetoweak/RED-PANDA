import os
import unittest
from unittest.mock import patch

from redpanda.sandbox.command import (
    BoundedTextCapture,
    CaptureLimit,
    strip_terminal_control,
)
from redpanda.sandbox.local.child_env import (
    CHILD_ENV_OVERLAY,
    latest_persistent_path,
)
from redpanda.sandbox.local.powershell import CommandEnvironmentPolicy


class ChildEnvOverlayTest(unittest.TestCase):
    def test_overlay_is_applied_to_child_environment(self):
        env = CommandEnvironmentPolicy().build({})
        for name, value in CHILD_ENV_OVERLAY.items():
            self.assertEqual(env[name], value)

    def test_path_is_replaced_with_latest_persistent_value(self):
        with patch(
            "redpanda.sandbox.local.powershell.latest_persistent_path",
            return_value="C:\\new;C:\\tools",
        ):
            env = CommandEnvironmentPolicy().build(
                {"PATH": "C:\\old", "SYSTEMROOT": "C:\\Windows"}
            )
        self.assertEqual(env["PATH"], "C:\\new;C:\\tools")

    def test_path_falls_back_to_host_snapshot_without_persistent_value(self):
        with patch(
            "redpanda.sandbox.local.powershell.latest_persistent_path",
            return_value=None,
        ):
            env = CommandEnvironmentPolicy().build({"PATH": "C:\\old"})
        self.assertEqual(env["PATH"], "C:\\old")

    def test_path_is_added_when_host_snapshot_lacks_it(self):
        with patch(
            "redpanda.sandbox.local.powershell.latest_persistent_path",
            return_value="C:\\new",
        ):
            env = CommandEnvironmentPolicy().build({"SYSTEMROOT": "C:\\Windows"})
        self.assertEqual(env["PATH"], "C:\\new")

    def test_explicit_path_override_wins_without_duplicate_windows_names(self):
        with patch(
            "redpanda.sandbox.local.powershell.latest_persistent_path",
            return_value="system-tools",
        ):
            env = CommandEnvironmentPolicy(fixed_values={"PATH": "work-tools"}).build({"Path": "host-tools"})

        self.assertEqual(env["PATH"], "work-tools")
        self.assertNotIn("Path", env)
        self.assertNotIn("VIRTUAL_ENV", env)

    def test_workspace_policy_preserves_activation_without_reading_registry(self):
        source = {"Path": "project-tools;system-tools", "CONDA_PREFIX": "project-env"}
        with patch("redpanda.sandbox.local.powershell.latest_persistent_path") as refresh:
            env = CommandEnvironmentPolicy(
                forward_names=tuple(source), refresh_path=False,
            ).build(source)

        refresh.assert_not_called()
        self.assertEqual(env["Path"], source["Path"])
        self.assertEqual(env["CONDA_PREFIX"], "project-env")

    @unittest.skipUnless(os.name == "nt", "Windows 注册表读取")
    def test_reads_real_persistent_path_on_windows(self):
        value = latest_persistent_path()
        self.assertIsInstance(value, str)
        self.assertTrue(value)


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
