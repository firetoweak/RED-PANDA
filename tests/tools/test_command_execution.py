from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from pydantic import ValidationError

from konden.filesystem import FileAccess
from konden.execution.api import CommandEnvironment
from konden.execution.command import (
    CaptureLimit, CapturedOutput, CommandResult, ShellNotFoundError,
)
from konden.filesystem.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from konden.tools.registry import ToolRegistry
from konden.tools.executor import ToolsExecutor
from konden.tools.builtin.command_execution import (
    CommandInput,
    create_command_execution_spec,
)
from konden.tools.spec import EmptyInput, pydantic_tool_spec
from konden.execution.local.powershell import (
    CommandEnvironmentPolicy,
    PowerShellCommandRunner,
)


POWERSHELL = shutil.which("pwsh.exe") or shutil.which("powershell.exe")


class ToolContractBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_pydantic_tool_input_is_strict_and_forbids_extra_fields(self):
        async def handler(value):
            return {"ok": True, "code": "OK", "data": value.model_dump()}

        registry = ToolRegistry()
        registry.register(
            pydantic_tool_spec(
                name="strict_command",
                description="strict input",
                input_model=CommandInput,
                handler=handler,
            )
        )
        executor = ToolsExecutor(registry)

        coerced = await executor.execute(
            "strict_command",
            '{"command":"ok","wait_seconds":"5"}',
        )
        extra = await executor.execute(
            "strict_command",
            '{"command":"ok","unexpected":true}',
        )

        self.assertEqual(coerced["code"], "VALIDATION_ERROR")
        self.assertEqual(extra["code"], "VALIDATION_ERROR")
        for bad in ({"wait_seconds": 31},):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                CommandInput.model_validate({"command": "ok", **bad})

    async def test_handler_unexpected_exception_becomes_tool_error(self):
        async def handler(_value):
            raise ValueError("empty pattern")

        registry = ToolRegistry()
        registry.register(
            pydantic_tool_spec(
                name="boom",
                description="raises inside handler",
                input_model=EmptyInput,
                handler=handler,
            )
        )
        executor = ToolsExecutor(registry)

        via_json = await executor.execute("boom", "{}")
        via_parsed = await executor.execute_parsed("boom", {})

        for result in (via_json, via_parsed):
            with self.subTest(path=result):
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "TOOL_HANDLER_ERROR")
                self.assertEqual(result["data"]["exception_type"], "ValueError")
                self.assertIn("ValueError: empty pattern", result["error"])

    async def test_handler_cancelled_error_still_propagates(self):
        async def handler(_value):
            raise asyncio.CancelledError()

        registry = ToolRegistry()
        registry.register(
            pydantic_tool_spec(
                name="cancel_me",
                description="cancelled handler",
                input_model=EmptyInput,
                handler=handler,
            )
        )
        executor = ToolsExecutor(registry)

        with self.assertRaises(asyncio.CancelledError):
            await executor.execute("cancel_me", "{}")
        with self.assertRaises(asyncio.CancelledError):
            await executor.execute_parsed("cancel_me", {})


class CommandEnvironmentPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_forwards_baseline_and_explicit_names(self):
        policy = CommandEnvironmentPolicy(
            forward_names=("HELPER_ALLOWED",),
            fixed_values={"HELPER_FIXED": "fixed"},
        )

        # PATH 的持久化重读由 test_child_env 覆盖；这里钉住白名单转发语义。
        with patch(
            "konden.execution.local.powershell.latest_persistent_path",
            return_value=None,
        ):
            child_env = policy.build({
                "PATH": "bin",
                "HELPER_ALLOWED": "allowed",
                "HELPER_SECRET": "secret",
            })

        self.assertEqual(child_env["PATH"], "bin")
        self.assertEqual(child_env["HELPER_ALLOWED"], "allowed")
        self.assertEqual(child_env["HELPER_FIXED"], "fixed")
        self.assertNotIn("HELPER_SECRET", child_env)

    async def test_environment_name_matching_is_case_insensitive(self):
        policy = CommandEnvironmentPolicy(forward_names=("helper_allowed",))

        with patch(
            "konden.execution.local.powershell.latest_persistent_path",
            return_value=None,
        ):
            child_env = policy.build({"HELPER_ALLOWED": "yes"})

        self.assertEqual(child_env["HELPER_ALLOWED"], "yes")


def _read_write_binding(root: Path, environment_id: str) -> FileAccess:
    return FileAccess(
        environment_id=environment_id,
        workspace_view=WorkspaceViewSnapshot((
            RootBinding("project", WorkspaceScope.TASK, root),
        )),
        permission_binding=PermissionBinding((('project', FilesystemPermission.READ_WRITE),)),
        cwd=root,
    )


class ExecuteCommandContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_shell_maps_to_shell_not_found(self):
        class MissingShellRunner:
            async def run(self, command, cwd, wait_seconds):
                raise ShellNotFoundError("bash", "/bin/bash")

        with tempfile.TemporaryDirectory() as directory:
            binding = _read_write_binding(Path(directory), 'linux-test')
            commands = CommandEnvironment(MissingShellRunner(), "bash", "/bin/bash")
            spec = create_command_execution_spec(binding, commands)

            result = await spec.handler(CommandInput(command="true"))

        self.assertEqual(result["code"], "SHELL_NOT_FOUND")
        self.assertEqual(result["shell"], "bash")
        self.assertEqual(result["executable"], "/bin/bash")

    async def test_exit_code_selects_result_code(self):
        class ExitRunner:
            def __init__(self, exit_code):
                self.exit_code = exit_code

            async def run(self, command, cwd, wait_seconds, **kwargs):
                empty = CapturedOutput("", 0, False, 0)
                return CommandResult(self.exit_code, empty, empty, 1, False)

        for exit_code, code in ((0, "COMMAND_COMPLETED"), (2, "COMMAND_EXIT_NONZERO")):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                binding = _read_write_binding(Path(directory), 'exit-test')
                runner = ExitRunner(exit_code)
                spec = create_command_execution_spec(
                    binding, CommandEnvironment(runner, "bash", "/bin/bash"),
                )
                result = await spec.handler(CommandInput(command="x"))
                self.assertTrue(result["ok"])
                self.assertEqual(result["code"], code)
                self.assertEqual(result["data"]["exit_code"], exit_code)

    async def test_unknown_executor_error_passes_through(self):
        class BrokenRunner:
            async def run(self, command, cwd, wait_seconds):
                raise RuntimeError("internal executor bug")

        with tempfile.TemporaryDirectory() as directory:
            binding = _read_write_binding(Path(directory), 'failure-test')
            commands = CommandEnvironment(BrokenRunner(), "bash", "/bin/bash")
            spec = create_command_execution_spec(binding, commands)

            with self.assertRaisesRegex(RuntimeError, "internal executor bug"):
                await spec.handler(CommandInput(command="true"))


class PowerShellDiscoveryTest(unittest.TestCase):
    def test_prefers_powershell_7(self):
        with patch(
            "konden.execution.local.powershell.shutil.which",
            side_effect=lambda name, *, path: {
                "pwsh.exe": "C:/PowerShell/7/pwsh.exe",
                "powershell.exe": "C:/Windows/powershell.exe",
            }.get(name),
        ):
            runner = PowerShellCommandRunner()

        self.assertEqual(runner.executable, "C:/PowerShell/7/pwsh.exe")

    def test_uses_windows_powershell_when_version_7_is_unavailable(self):
        with patch(
            "konden.execution.local.powershell.shutil.which",
            side_effect=lambda name, *, path: {
                "powershell.exe": "C:/Windows/powershell.exe",
            }.get(name),
        ):
            runner = PowerShellCommandRunner()

        self.assertEqual(runner.executable, "C:/Windows/powershell.exe")

    def test_fails_when_no_powershell_is_available(self):
        with patch(
            "konden.execution.local.powershell.shutil.which",
            return_value=None,
        ):
            with self.assertRaisesRegex(
                ShellNotFoundError,
                "pwsh.exe / powershell.exe",
            ):
                PowerShellCommandRunner()


@unittest.skipUnless(POWERSHELL, "需要 Windows PowerShell")
@pytest.mark.process
class PowerShellCommandRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_stdout_stderr_and_explicit_exit_code(self):
        runner = PowerShellCommandRunner()

        result = await runner.run(
            '[Console]::Out.Write("out"); '
            '[Console]::Error.Write("err"); exit 7',
            Path.cwd(),
            10,
        )

        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stdout.content, "out")
        self.assertEqual(result.stderr.content, "err")
        self.assertFalse(result.timed_out)

    async def test_preserves_native_program_exit_code(self):
        runner = PowerShellCommandRunner()

        result = await runner.run("cmd /c exit 2", Path.cwd(), 10)

        self.assertEqual(result.exit_code, 2)

    async def test_decodes_utf8_output(self):
        runner = PowerShellCommandRunner()
        expected = "".join(chr(code) for code in (0x4E2D, 0x6587))

        result = await runner.run(
            f'[Console]::Out.Write("{expected}")',
            Path.cwd(),
            10,
        )

        self.assertEqual(result.stdout.content, expected)

    async def test_does_not_inherit_unlisted_host_variable(self):
        runner = PowerShellCommandRunner()
        name = "HELPER_COMMAND_SECRET_TEST"
        previous = os.environ.get(name)
        os.environ[name] = "must-not-leak"
        try:
            result = await runner.run(
                f'if (Test-Path Env:{name}) {{ Write-Output "leaked" }} '
                'else { Write-Output "hidden" }',
                Path.cwd(),
                10,
            )
        finally:
            if previous is None:
                del os.environ[name]
            else:
                os.environ[name] = previous

        self.assertEqual(result.stdout.content.strip(), "hidden")

    async def test_large_output_keeps_head_and_tail(self):
        runner = PowerShellCommandRunner(
            capture_limit=CaptureLimit(max_chars=100, head_chars=40)
        )

        result = await runner.run(
            '[Console]::Out.Write(("A" * 8000) + "TAIL")',
            Path.cwd(),
            10,
        )

        self.assertTrue(result.stdout.truncated)
        self.assertEqual(result.stdout.total_chars, 8004)
        self.assertEqual(result.stdout.omitted_chars, 7904)
        self.assertTrue(result.stdout.content.startswith("A" * 40))
        self.assertTrue(result.stdout.content.endswith("TAIL"))

    async def test_timeout_returns_partial_result_and_stops_waiting(self):
        runner = PowerShellCommandRunner()

        result = await runner.run(
            '[Console]::Out.WriteLine("started"); Start-Sleep -Seconds 5',
            Path.cwd(),
            1,
        )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertIn("started", result.stdout.content)
        self.assertLess(result.duration_ms, 4_000)

    async def test_timeout_terminates_child_process_tree(self):
        runner = PowerShellCommandRunner()
        executable = str(runner.executable).replace("'", "''")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "child-finished.txt"
            script = root / "child.ps1"
            script.write_text(
                "Start-Sleep -Seconds 2\n"
                f"Set-Content -LiteralPath '{marker}' -Value done\n",
                encoding="utf-8",
            )

            result = await runner.run(
                f"& '{executable}' -NoProfile -File '{script}'",
                root,
                0.2,
            )
            time.sleep(2.5)

            self.assertTrue(result.timed_out)
            self.assertFalse(marker.exists())

    async def test_task_cancellation_terminates_child_process_tree(self):
        runner = PowerShellCommandRunner()
        executable = str(runner.executable).replace("'", "''")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "child-finished.txt"
            script = root / "child.ps1"
            script.write_text(
                "Start-Sleep -Seconds 2\n"
                f"Set-Content -LiteralPath '{marker}' -Value done\n",
                encoding="utf-8",
            )
            task = asyncio.create_task(
                runner.run(
                    f"& '{executable}' -NoProfile -File '{script}'",
                    root,
                    10,
                )
            )
            await asyncio.sleep(0.2)
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task

            await asyncio.sleep(2.5)
            self.assertFalse(marker.exists())

    async def test_interrupt_terminates_child_process_tree_and_keeps_output(self):
        runner = PowerShellCommandRunner()
        executable = str(runner.executable).replace("'", "''")
        interrupt = asyncio.Event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "child-finished.txt"
            script = root / "child.ps1"
            script.write_text(
                "Write-Output started\n"
                "Start-Sleep -Seconds 2\n"
                f"Set-Content -LiteralPath '{marker}' -Value done\n",
                encoding="utf-8",
            )
            task = asyncio.create_task(
                runner.run(
                    "[Console]::Out.WriteLine('started'); "
                    "[Console]::Out.Flush(); "
                    f"& '{executable}' -NoProfile -File '{script}'",
                    root,
                    30,
                    interrupt=interrupt,
                )
            )
            await asyncio.sleep(1)
            interrupt.set()
            result = await task
            await asyncio.sleep(2.5)

            self.assertTrue(result.interrupted)
            self.assertFalse(result.timed_out)
            self.assertIsNone(result.exit_code)
            self.assertIn("started", result.stdout.content)
            self.assertFalse(marker.exists())

    async def test_interrupt_returns_only_after_killed_descendant_has_exited(self):
        import ctypes
        import sys

        runner = PowerShellCommandRunner()
        interrupt = asyncio.Event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"
            (root / "writer.py").write_text(
                "import os, pathlib, time\n"
                "held = bytearray(300 * 1024 * 1024)\n"
                "pathlib.Path('ready.tmp').write_text(str(os.getpid()))\n"
                "os.replace('ready.tmp', 'ready')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            # The writer holds none of the command's output pipes, so draining
            # them cannot stand in for waiting on its exit.
            (root / "spawn.py").write_text(
                "import subprocess, sys\n"
                "N = subprocess.DEVNULL\n"
                "subprocess.run([sys.executable, 'writer.py'], stdin=N, stdout=N, stderr=N)\n",
                encoding="utf-8",
            )
            executable = sys.executable.replace("'", "''")
            task = asyncio.create_task(runner.run(
                f"& '{executable}' spawn.py", root, 60, interrupt=interrupt,
            ))
            while not ready.exists():
                self.assertFalse(task.done(), "command ended before the writer started")
                await asyncio.sleep(0.01)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x100000, False, int(ready.read_text()))
            self.assertTrue(handle)
            try:
                interrupt.set()
                result = await task
                self.assertTrue(result.interrupted)
                # A killed process leaves the Job before it has closed its
                # handles; the interrupt must outlast the process itself.
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0)
            finally:
                kernel.CloseHandle(handle)

    async def test_normal_completion_terminates_background_process_tree(self):
        runner = PowerShellCommandRunner()
        executable = str(runner.executable).replace("'", "''")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "child-finished.txt"
            script = root / "child.ps1"
            script.write_text(
                "Start-Sleep -Seconds 2\n"
                f"Set-Content -LiteralPath '{marker}' -Value done\n",
                encoding="utf-8",
            )

            result = await runner.run(
                f"Start-Process -FilePath '{executable}' "
                f"-ArgumentList '-NoProfile','-File','{script}' "
                "-WindowStyle Hidden",
                root,
                10,
            )
            await asyncio.sleep(2.5)

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertFalse(marker.exists())

    async def test_missing_powershell_is_an_expected_boundary_error(self):
        runner = PowerShellCommandRunner(
            executable="missing-powershell-for-konden-test.exe"
        )

        with self.assertRaisesRegex(FileNotFoundError, "未找到 Shell"):
            await runner.run("Write-Output ok", Path.cwd(), 10)


@unittest.skipUnless(POWERSHELL, "需要 Windows PowerShell")
@pytest.mark.process
class ExecuteCommandToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.directory.name)
        view = WorkspaceViewSnapshot((
            RootBinding("project", WorkspaceScope.TASK, self.workspace_root),
        ))
        runner = PowerShellCommandRunner()
        self.binding = FileAccess(
            environment_id='local-test',
            workspace_view=view,
            permission_binding=PermissionBinding((('project', FilesystemPermission.READ_WRITE),)),
            cwd=self.workspace_root,
        )
        self.commands = CommandEnvironment(runner, "powershell", POWERSHELL, background_supported=False)
        registry = ToolRegistry()
        registry.register(
            create_command_execution_spec(
                self.binding, self.commands,
            )
        )
        self.executor = ToolsExecutor(registry)

    def tearDown(self):
        self.directory.cleanup()

    async def execute(self, payload: dict) -> dict:
        return await self.executor.execute(
            "execute_command",
            json.dumps(payload, ensure_ascii=False),
        )

    async def test_nonzero_exit_reports_exit_nonzero(self):
        result = await self.execute({
            "command": 'Write-Error "failed"',
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "COMMAND_EXIT_NONZERO")
        self.assertEqual(result["data"]["exit_code"], 1)
        self.assertIn("failed", result["data"]["stderr"]["content"])

    async def test_permission_binding_rejects_potential_write_command(self):
        read_only_binding = FileAccess(
            environment_id=self.binding.environment_id,
            workspace_view=self.binding.workspace_view,
            permission_binding=PermissionBinding((('project', FilesystemPermission.READ_ONLY),)),
            cwd=self.binding.cwd,
        )
        read_only_commands = CommandEnvironment(self.commands.command_executor, self.commands.shell_name, self.commands.shell_path)
        registry = ToolRegistry()
        registry.register(create_command_execution_spec(read_only_binding, read_only_commands))
        executor = ToolsExecutor(registry)

        denied = await executor.execute(
            "execute_command",
            json.dumps({"command": "Write-Output ok"}),
        )

        self.assertEqual(denied["code"], "ENVIRONMENT_PERMISSION_DENIED")

    async def test_runs_in_workspace_relative_cwd(self):
        child = self.workspace_root / "child"
        child.mkdir()

        result = await self.execute({
            "cwd": "child",
            "command": "(Get-Location).Path",
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["cwd"], "child")
        self.assertEqual(
            Path(result["data"]["stdout"]["content"].strip()).resolve(),
            child.resolve(),
        )

    async def test_accepts_absolute_environment_cwd_and_rejects_escape(self):
        absolute = await self.execute({
            "cwd": str(self.workspace_root),
            "command": "Write-Output ok",
        })
        escaping = await self.execute({
            "cwd": "../outside",
            "command": "Write-Output ok",
        })

        self.assertEqual(absolute["code"], "COMMAND_COMPLETED")
        self.assertEqual(escaping["code"], "PATH_OUTSIDE_WORKSPACE_VIEW")

    async def test_command_may_use_an_absolute_path_outside_workspace(self):
        with tempfile.TemporaryDirectory() as outside_directory:
            outside_file = Path(outside_directory) / "outside.txt"
            outside_file.write_text("outside-fact", encoding="utf-8")
            literal_path = str(outside_file).replace("'", "''")

            result = await self.execute({
                "command": f"Get-Content -LiteralPath '{literal_path}' -Raw",
            })

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["data"]["stdout"]["content"].strip(),
            "outside-fact",
        )

    async def test_empty_command_is_rejected(self):
        result = await self.execute({"command": "  "})

        self.assertEqual(result["code"], "EMPTY_COMMAND")
