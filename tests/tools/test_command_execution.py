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

from redpanda.sandbox.api import (
    EnvironmentBinding,
    ExecutionAttachment,
)
from redpanda.sandbox.command import CaptureLimit, ShellNotFoundError
from redpanda.sandbox.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from redpanda.tools.registry import ToolRegistry
from redpanda.tools.executor import ToolsExecutor
from redpanda.tools.builtin.command_execution import (
    ExecuteCommandInput,
    create_command_execution_spec,
)
from redpanda.tools.spec import EmptyInput, pydantic_tool_spec
from redpanda.sandbox.local.powershell import (
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
                input_model=ExecuteCommandInput,
                handler=handler,
            )
        )
        executor = ToolsExecutor(registry)

        coerced = await executor.execute(
            "strict_command",
            '{"command":"ok","timeout_seconds":"5"}',
        )
        extra = await executor.execute(
            "strict_command",
            '{"command":"ok","unexpected":true}',
        )

        self.assertEqual(coerced["code"], "VALIDATION_ERROR")
        self.assertEqual(extra["code"], "VALIDATION_ERROR")

    async def test_tool_registry_rejects_duplicate_names(self):
        async def handler(_value):
            return {"ok": True, "code": "OK"}

        spec = pydantic_tool_spec(
            name="duplicate",
            description="duplicate guard",
            input_model=EmptyInput,
            handler=handler,
        )
        registry = ToolRegistry()
        registry.register(spec)
        with self.assertRaises(ValueError):
            registry.register(spec)


class CommandEnvironmentPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_forwards_baseline_and_explicit_names(self):
        policy = CommandEnvironmentPolicy(
            forward_names=("HELPER_ALLOWED",),
            fixed_values={"HELPER_FIXED": "fixed"},
        )

        # PATH 的持久化重读由 test_child_env 覆盖；这里钉住白名单转发语义。
        with patch(
            "redpanda.sandbox.local.powershell.latest_persistent_path",
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
            "redpanda.sandbox.local.powershell.latest_persistent_path",
            return_value=None,
        ):
            child_env = policy.build({"HELPER_ALLOWED": "yes"})

        self.assertEqual(child_env["HELPER_ALLOWED"], "yes")


class ExecuteCommandContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_binding_shell_metadata_and_maps_missing_shell(self):
        class MissingShellRunner:
            async def run(self, command, cwd, timeout_seconds):
                raise ShellNotFoundError("bash", "/bin/bash")

        with tempfile.TemporaryDirectory() as directory:
            workspace_root = Path(directory)
            view = WorkspaceViewSnapshot((
                RootBinding("project", WorkspaceScope.TASK, workspace_root),
            ))
            binding = EnvironmentBinding(
                environment_id="linux-test",
                workspace_view=view,
                permission_binding=PermissionBinding((
                    ("project", FilesystemPermission.READ_WRITE),
                )),
                cwd=workspace_root,
                shell_name="bash",
                shell_path="/bin/bash",
                execution_attachment=ExecutionAttachment(
                    "linux-test",
                    MissingShellRunner(),
                ),
            )
            spec = create_command_execution_spec(binding)

            result = await spec.handler(ExecuteCommandInput(
                command="true",
                workspace_effect="read_only",
            ))

        self.assertIn("使用 bash", spec.description)
        self.assertIn("/bin/bash", spec.description)
        command_schema = spec.to_openai_tool()["function"]["parameters"][
            "properties"
        ]["command"]
        self.assertIn("交给 Shell 执行", command_schema["description"])
        self.assertNotIn("PowerShell", command_schema["description"])
        self.assertEqual(result["code"], "SHELL_NOT_FOUND")
        self.assertEqual(result["shell"], "bash")
        self.assertEqual(result["executable"], "/bin/bash")

    async def test_unknown_executor_error_passes_through(self):
        class BrokenRunner:
            async def run(self, command, cwd, timeout_seconds):
                raise RuntimeError("internal executor bug")

        with tempfile.TemporaryDirectory() as directory:
            workspace_root = Path(directory)
            view = WorkspaceViewSnapshot((
                RootBinding("project", WorkspaceScope.TASK, workspace_root),
            ))
            binding = EnvironmentBinding(
                environment_id="failure-test",
                workspace_view=view,
                permission_binding=PermissionBinding((
                    ("project", FilesystemPermission.READ_WRITE),
                )),
                cwd=workspace_root,
                shell_name="bash",
                shell_path="/bin/bash",
                execution_attachment=ExecutionAttachment(
                    "failure-test",
                    BrokenRunner(),
                ),
            )
            spec = create_command_execution_spec(binding)

            with self.assertRaisesRegex(RuntimeError, "internal executor bug"):
                await spec.handler(ExecuteCommandInput(command="true"))


class PowerShellDiscoveryTest(unittest.TestCase):
    def test_prefers_powershell_7(self):
        with patch(
            "redpanda.sandbox.local.powershell.shutil.which",
            side_effect=lambda name: {
                "pwsh.exe": "C:/PowerShell/7/pwsh.exe",
                "powershell.exe": "C:/Windows/powershell.exe",
            }.get(name),
        ):
            runner = PowerShellCommandRunner()

        self.assertEqual(runner.executable, "C:/PowerShell/7/pwsh.exe")

    def test_uses_windows_powershell_when_version_7_is_unavailable(self):
        with patch(
            "redpanda.sandbox.local.powershell.shutil.which",
            side_effect=lambda name: {
                "powershell.exe": "C:/Windows/powershell.exe",
            }.get(name),
        ):
            runner = PowerShellCommandRunner()

        self.assertEqual(runner.executable, "C:/Windows/powershell.exe")

    def test_fails_when_no_powershell_is_available(self):
        with patch(
            "redpanda.sandbox.local.powershell.shutil.which",
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

    async def test_uses_powershell_composition_semantics(self):
        runner = PowerShellCommandRunner()

        result = await runner.run(
            'Write-Output "alpha" | ForEach-Object { "$_-beta" }',
            Path.cwd(),
            10,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.content.strip(), "alpha-beta")

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
            executable="missing-powershell-for-redpanda-test.exe"
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
        self.binding = EnvironmentBinding(
            environment_id="local-test",
            workspace_view=view,
            permission_binding=PermissionBinding((
                ("project", FilesystemPermission.READ_WRITE),
            )),
            cwd=self.workspace_root,
            shell_name="powershell",
            shell_path=POWERSHELL,
            execution_attachment=ExecutionAttachment("local-test", runner),
        )
        registry = ToolRegistry()
        registry.register(
            create_command_execution_spec(
                self.binding,
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

    async def test_nonzero_exit_is_a_completed_command(self):
        result = await self.execute({
            "command": 'Write-Error "failed"',
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "COMMAND_COMPLETED")
        self.assertEqual(result["data"]["exit_code"], 1)
        self.assertIn("failed", result["data"]["stderr"]["content"])

    async def test_workspace_effect_defaults_to_may_write(self):
        result = await self.execute({
            "command": "Write-Output ok",
        })

        self.assertEqual(result["data"]["workspace_effect"], "may_write")

    async def test_read_only_workspace_effect_is_returned(self):
        result = await self.execute({
            "command": "Write-Output ok",
            "workspace_effect": "read_only",
        })

        self.assertEqual(result["data"]["workspace_effect"], "read_only")

    async def test_permission_binding_rejects_potential_write_command(self):
        read_only_binding = EnvironmentBinding(
            environment_id=self.binding.environment_id,
            workspace_view=self.binding.workspace_view,
            permission_binding=PermissionBinding((
                ("project", FilesystemPermission.READ_ONLY),
            )),
            cwd=self.binding.cwd,
            shell_name=self.binding.shell_name,
            shell_path=self.binding.shell_path,
            execution_attachment=self.binding.execution_attachment,
        )
        registry = ToolRegistry()
        registry.register(create_command_execution_spec(read_only_binding))
        executor = ToolsExecutor(registry)

        denied = await executor.execute(
            "execute_command",
            json.dumps({"command": "Write-Output ok"}),
        )
        allowed = await executor.execute(
            "execute_command",
            json.dumps({
                "command": "Write-Output ok",
                "workspace_effect": "read_only",
            }),
        )

        self.assertEqual(denied["code"], "ENVIRONMENT_PERMISSION_DENIED")
        self.assertEqual(allowed["code"], "COMMAND_COMPLETED")

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

    async def test_timeout_is_a_tool_failure_with_partial_output(self):
        result = await self.execute({
            "command": (
                '[Console]::Out.WriteLine("started"); '
                "Start-Sleep -Seconds 5"
            ),
            "timeout_seconds": 2,
        })

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "COMMAND_TIMEOUT")
        self.assertTrue(result["data"]["timed_out"])
        self.assertIn("started", result["data"]["stdout"]["content"])

    async def test_timeout_schema_limits_are_exposed_and_validated(self):
        schema = ExecuteCommandInput.model_json_schema()["properties"]
        self.assertEqual(schema["timeout_seconds"]["minimum"], 1)
        self.assertEqual(schema["timeout_seconds"]["maximum"], 300)
        with self.assertRaises(ValidationError):
            ExecuteCommandInput.model_validate({
                "command": "ok",
                "timeout_seconds": 301,
            })
        with self.assertRaises(ValidationError):
            ExecuteCommandInput.model_validate({
                "command": "ok",
                "workspace_effect": "unknown",
            })
