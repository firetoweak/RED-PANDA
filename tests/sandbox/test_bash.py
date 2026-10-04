from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from redpanda.sandbox.command import CaptureLimit, ShellNotFoundError
from redpanda.sandbox.local.bash import (
    BashCommandEnvironmentPolicy,
    BashCommandRunner,
)
from redpanda.sandbox.local.provider import create_local_environment_provider


BASH = shutil.which("bash") if os.name == "posix" else None


class BashCommandEnvironmentPolicyTest(unittest.TestCase):
    def test_only_forwards_posix_baseline_and_explicit_names(self):
        policy = BashCommandEnvironmentPolicy(
            forward_names=("HELPER_ALLOWED",),
            fixed_values={"HELPER_FIXED": "fixed"},
        )

        child_env = policy.build({
            "HOME": "/home/agent",
            "PATH": "/usr/bin",
            "HELPER_ALLOWED": "allowed",
            "HELPER_SECRET": "secret",
        })

        self.assertEqual(child_env["HOME"], "/home/agent")
        self.assertEqual(child_env["PATH"], "/usr/bin")
        self.assertEqual(child_env["HELPER_ALLOWED"], "allowed")
        self.assertEqual(child_env["HELPER_FIXED"], "fixed")
        self.assertNotIn("HELPER_SECRET", child_env)

    def test_environment_name_matching_is_case_sensitive(self):
        policy = BashCommandEnvironmentPolicy(forward_names=("helper_allowed",))

        child_env = policy.build({"HELPER_ALLOWED": "no"})

        self.assertNotIn("HELPER_ALLOWED", child_env)


class BashDiscoveryTest(unittest.TestCase):
    def test_discovers_bash(self):
        with patch(
            "redpanda.sandbox.local.bash.shutil.which",
            return_value="/usr/bin/bash",
        ):
            runner = BashCommandRunner()

        self.assertEqual(runner.executable, "/usr/bin/bash")

    def test_fails_when_bash_is_unavailable(self):
        with patch(
            "redpanda.sandbox.local.bash.shutil.which",
            return_value=None,
        ):
            with self.assertRaisesRegex(ShellNotFoundError, "bash"):
                BashCommandRunner()


class LocalEnvironmentProviderSelectionTest(unittest.TestCase):
    def test_selects_bash_for_non_windows(self):
        with (
            patch("redpanda.sandbox.local.provider.os.name", "other"),
            patch(
                "redpanda.sandbox.local.bash.BashCommandRunner"
            ) as runner_type,
        ):
            runner_type.return_value.executable = "/usr/bin/bash"
            provider = create_local_environment_provider()

        self.assertIs(provider.command_executor, runner_type.return_value)
        self.assertEqual(provider.shell_name, "bash")
        self.assertEqual(provider.shell_path, "/usr/bin/bash")

    def test_selects_powershell_for_windows(self):
        with (
            patch("redpanda.sandbox.local.provider.os.name", "nt"),
            patch(
                "redpanda.sandbox.local.powershell.PowerShellCommandRunner"
            ) as runner_type,
        ):
            runner_type.return_value.executable = "C:/PowerShell/7/pwsh.exe"
            provider = create_local_environment_provider()

        self.assertIs(provider.command_executor, runner_type.return_value)
        self.assertEqual(provider.shell_name, "powershell")
        self.assertEqual(provider.shell_path, "C:/PowerShell/7/pwsh.exe")


class BashFailureContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_process_start_error_passes_through(self):
        runner = BashCommandRunner(executable="/usr/bin/bash")

        with (
            patch(
                "redpanda.sandbox.local.bash.shutil.which",
                return_value="/usr/bin/bash",
            ),
            patch(
                "redpanda.sandbox.local.bash.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=RuntimeError("internal bug")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "internal bug"):
                await runner.run("printf ok", Path.cwd(), 10)


@unittest.skipUnless(BASH, "需要 POSIX Bash")
@pytest.mark.process
class BashCommandRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_stdout_stderr_and_explicit_exit_code(self):
        runner = BashCommandRunner()

        result = await runner.run(
            "printf stdout; printf stderr >&2; exit 7",
            Path.cwd(),
            10,
        )

        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stdout.content, "stdout")
        self.assertEqual(result.stderr.content, "stderr")
        self.assertFalse(result.timed_out)

    async def test_uses_bash_composition_semantics_and_cwd(self):
        runner = BashCommandRunner()
        with tempfile.TemporaryDirectory() as directory:
            result = await runner.run(
                "value=left; value+=right; printf '%s|%s' \"$value\" \"$PWD\"",
                Path(directory),
                10,
            )

        value, cwd = result.stdout.content.split("|", 1)
        self.assertEqual(value, "leftright")
        self.assertEqual(Path(cwd).resolve(), Path(directory).resolve())

    async def test_bounds_stdout_and_stderr(self):
        runner = BashCommandRunner(
            capture_limit=CaptureLimit(max_chars=12, head_chars=5),
        )

        result = await runner.run(
            "printf 12345678901234567890; printf abcdefghijklmnopqrst >&2",
            Path.cwd(),
            10,
        )

        self.assertTrue(result.stdout.truncated)
        self.assertTrue(result.stderr.truncated)
        self.assertEqual(result.stdout.total_chars, 20)
        self.assertEqual(result.stderr.total_chars, 20)
        self.assertTrue(result.stdout.content.startswith("12345"))
        self.assertTrue(result.stdout.content.endswith("4567890"))

    async def test_timeout_terminates_child_process_group(self):
        runner = BashCommandRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await runner.run(
                "(sleep 2; printf leaked > child.txt) & printf started; wait",
                root,
                1,
            )
            await asyncio.sleep(1.2)

            self.assertTrue(result.timed_out)
            self.assertIsNone(result.exit_code)
            self.assertIn("started", result.stdout.content)
            self.assertFalse((root / "child.txt").exists())

    async def test_cancellation_terminates_child_process_group(self):
        runner = BashCommandRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = asyncio.create_task(runner.run(
                "(sleep 1; printf leaked > child.txt) & wait",
                root,
                10,
            ))
            await asyncio.sleep(0.1)
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(1.1)

            self.assertFalse((root / "child.txt").exists())

    async def test_missing_bash_is_an_expected_boundary_error(self):
        runner = BashCommandRunner(
            executable="missing-bash-for-redpanda-test"
        )

        with self.assertRaisesRegex(FileNotFoundError, "未找到 Shell"):
            await runner.run("printf ok", Path.cwd(), 10)
