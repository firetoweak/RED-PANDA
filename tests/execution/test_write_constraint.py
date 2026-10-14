"""平台中立的任务写入约束：宿主拒写根、可能写入时的挂载目录、失败类别。"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from redpanda.execution.api import CommandEnvironment
from redpanda.execution.command import (
    BoundedTextCapture,
    CaptureLimit,
    CommandConstraintError,
    CommandResult,
    CommandStartError,
)
from redpanda.execution.constraint import (
    CommandConstraint,
    ConstraintFailure,
    TaskWriteConstraint,
    command_directory,
)
from redpanda.execution.local import windows_task
from redpanda.filesystem import FileAccess
from redpanda.filesystem.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from redpanda.tools.builtin.command_execution import (
    ExecuteCommandInput,
    create_command_execution_spec,
)


def _result() -> CommandResult:
    empty = BoundedTextCapture(CaptureLimit()).finish()
    return CommandResult(0, empty, empty, 1, False)


class TaskWriteConstraintTest(unittest.TestCase):
    def test_may_write_keeps_the_mount_and_the_host_root(self):
        host = Path("/host/task")
        mount = Path("/mnt/view")
        bound = TaskWriteConstraint(host).for_command(mount, may_write=True)

        self.assertEqual(bound.deny_root, host)
        self.assertEqual(bound.cwd, mount)
        self.assertTrue(bound.may_write)
        self.assertEqual(command_directory(bound), mount)

    def test_read_only_may_start_in_the_host_root(self):
        host = Path("/host/task")
        bound = TaskWriteConstraint(host).for_command(host / "src", may_write=False)

        self.assertEqual(bound.cwd, host / "src")
        self.assertFalse(bound.may_write)

    def test_may_write_inside_the_host_root_is_rejected(self):
        host = Path("/host/task")
        with self.assertRaisesRegex(RuntimeError, "挂载"):
            TaskWriteConstraint(host).for_command(host / "src", may_write=True)

    def test_relative_root_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            TaskWriteConstraint(Path("task"))


class ConstraintFailureTest(unittest.TestCase):
    def test_windows_protocol_maps_outside_the_shared_error(self):
        self.assertIs(
            windows_task.constraint_failure(3, 50),
            ConstraintFailure.UNSUPPORTED_PROCESS,
        )
        self.assertIs(
            windows_task.constraint_failure(3, 5),
            ConstraintFailure.NOT_HELD,
        )
        self.assertIs(
            windows_task.constraint_failure(4, 50),
            ConstraintFailure.NOT_HELD,
        )

    def test_shared_error_does_not_accept_windows_status_codes(self):
        with self.assertRaises(TypeError):
            CommandConstraintError(_result(), 3)  # type: ignore[arg-type]

    def test_unsupported_process_is_a_failure_kind(self):
        error = CommandConstraintError(_result(), ConstraintFailure.UNSUPPORTED_PROCESS)
        self.assertTrue(error.unsupported_process)
        held = CommandConstraintError(_result(), ConstraintFailure.NOT_HELD)
        self.assertFalse(held.unsupported_process)
        self.assertIs(held.result.exit_code, 0)


def _binding(root: Path, *, filesystem=None) -> FileAccess:
    return FileAccess(
        environment_id="local-test",
        workspace_view=WorkspaceViewSnapshot((
            RootBinding("project", WorkspaceScope.TASK, root),
        )),
        permission_binding=PermissionBinding((
            ("project", FilesystemPermission.READ_WRITE),
        )),
        cwd=root,
        filesystem=filesystem,
    )


class CommandLocationWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_file_access_and_runner_share_one_constraint(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            mount = Path(tempfile.mkdtemp()).resolve()
            self.addCleanup(shutil.rmtree, mount, ignore_errors=True)
            (mount / "child").mkdir()

            class Files:
                def native_path(self, logical: Path) -> Path:
                    return mount / logical.relative_to(host)

                def logical_path(self, native: Path) -> Path:
                    return native

            seen = {}

            class RecordingRunner:
                async def run(self, command, cwd, timeout_seconds, interrupt=None):
                    seen["location"] = cwd
                    empty = BoundedTextCapture(CaptureLimit()).finish()
                    return CommandResult(0, empty, empty, 1, False)

            constraint = TaskWriteConstraint(host)
            binding = _binding(host, filesystem=Files())
            commands = CommandEnvironment(
                RecordingRunner(), "bash", "/bin/bash", write_constraint=constraint,
            )
            spec = create_command_execution_spec(binding, commands)
            result = await spec.handler(ExecuteCommandInput(
                command="printf ok",
                cwd="child",
                workspace_effect="may_write",
            ))

        self.assertEqual(result["code"], "COMMAND_COMPLETED")
        location = seen["location"]
        self.assertIsInstance(location, CommandConstraint)
        self.assertEqual(location.deny_root, host.resolve())
        self.assertEqual(location.cwd, mount / "child")
        self.assertTrue(location.may_write)
        self.assertIs(commands.write_constraint, constraint)

    async def test_read_only_constraint_uses_the_host_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            seen = {}

            class RecordingRunner:
                async def run(self, command, cwd, timeout_seconds, interrupt=None):
                    seen["location"] = cwd
                    empty = BoundedTextCapture(CaptureLimit()).finish()
                    return CommandResult(0, empty, empty, 1, False)

            binding = _binding(host)
            commands = CommandEnvironment(
                RecordingRunner(),
                "bash",
                "/bin/bash",
                write_constraint=TaskWriteConstraint(host),
            )
            spec = create_command_execution_spec(binding, commands)
            result = await spec.handler(ExecuteCommandInput(
                command="printf ok",
                workspace_effect="read_only",
            ))

        self.assertEqual(result["code"], "COMMAND_COMPLETED")
        location = seen["location"]
        self.assertEqual(location.deny_root, host.resolve())
        self.assertEqual(location.cwd, host.resolve())
        self.assertFalse(location.may_write)

    async def test_constraint_must_match_the_file_access_root(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            other = host / "other"
            other.mkdir()
            binding = _binding(host)
            commands = CommandEnvironment(
                object(),
                "bash",
                "/bin/bash",
                write_constraint=TaskWriteConstraint(other),
            )
            spec = create_command_execution_spec(binding, commands)
            with self.assertRaisesRegex(RuntimeError, "宿主任务根"):
                await spec.handler(ExecuteCommandInput(command="printf ok"))

    def test_task_deny_root_requires_one_task_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            package.mkdir()
            project = root / "project"
            project.mkdir()
            binding = FileAccess(
                environment_id="local-test",
                workspace_view=WorkspaceViewSnapshot((
                    RootBinding("project", WorkspaceScope.TASK, project),
                    RootBinding("package", WorkspaceScope.TASK, package),
                )),
                permission_binding=PermissionBinding((
                    ("project", FilesystemPermission.READ_WRITE),
                    ("package", FilesystemPermission.READ_WRITE),
                )),
                cwd=project,
            )
            with self.assertRaisesRegex(RuntimeError, "唯一"):
                binding.task_deny_root()


class PowerShellConstraintGateTest(unittest.TestCase):
    def test_workspace_runner_refuses_to_start_without_the_constraint(self):
        from redpanda.execution.local.powershell import PowerShellCommandRunner

        with patch(
            "redpanda.execution.local.powershell.shutil.which",
            return_value="pwsh.exe",
        ):
            runner = PowerShellCommandRunner(
                executable="pwsh.exe",
                write_constraint=TaskWriteConstraint(Path("/host/task")),
            )
            with self.assertRaisesRegex(CommandStartError, "缺少写入约束"):
                asyncio.run(runner.run("Write-Output ok", Path("/host/task"), 10))
