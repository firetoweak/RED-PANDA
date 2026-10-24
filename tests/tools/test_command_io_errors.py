import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from konden.execution.api import CommandEnvironment
from konden.execution.local.bash import BashCommandRunner
from konden.execution.local.powershell import PowerShellCommandRunner
from konden.tools.builtin.command_execution import (
    CommandInput,
    create_command_execution_spec,
)


class CommandIoErrorsTest(unittest.IsolatedAsyncioTestCase):
    def process(self):
        return SimpleNamespace(
            pid=123, returncode=1,
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
            stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
            stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
            wait=AsyncMock(return_value=1),
        )

    async def test_powershell_broken_stdin_is_failure_after_cleanup(self):
        proc = self.process()
        proc.stdin.drain.side_effect = BrokenPipeError("child exited")
        job = Mock()
        with patch("konden.execution.local.powershell.shutil.which", return_value="powershell"), \
             patch("konden.execution.local.powershell.WindowsJob.create", return_value=job), \
             patch("konden.execution.local.powershell.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await PowerShellCommandRunner(executable="powershell").run("x", Path.cwd(), 10)
        job.close.assert_called_once()
        self.assertIn("stdin: BrokenPipeError", result.io_errors[0])
        self.assertFalse(result.timed_out)

        resolved = SimpleNamespace(
            native_path=Path.cwd(),
            workspace_membership=SimpleNamespace(display_path="."),
        )
        runner = SimpleNamespace(run=AsyncMock(return_value=result))
        binding = SimpleNamespace(
            resolver=SimpleNamespace(resolve=lambda *args, **kwargs: resolved),
        )
        outcome = await create_command_execution_spec(binding, CommandEnvironment(runner, "powershell", "powershell")).handler(CommandInput(command="x"))
        self.assertEqual(outcome["code"], "COMMAND_IO_FAILED")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["data"]["io_errors"], list(result.io_errors))
        runner.run.assert_awaited_once()

    async def test_bash_read_failure_keeps_partial_output(self):
        if sys.platform == "linux":
            stdout = MagicMock()
            stdout.__enter__.return_value = stdout
            stdout.read.side_effect = [b"partial", OSError("output read failed")]
            stderr = MagicMock()
            stderr.__enter__.return_value = stderr
            stderr.read.return_value = b""

            def open_output(path, *args, **kwargs):
                return stdout if path.name == "stdout.bin" else stderr

            running = Mock(poll=Mock(return_value=1))
            with patch("konden.execution.local.bash.shutil.which", return_value="bash"), \
                 patch("konden.execution.local.owned_command.Running", return_value=running), \
                 patch("konden.execution.local.owned_command.Path.open", new=open_output):
                result = await BashCommandRunner(executable="bash").run("x", Path.cwd(), 10)
            running.close.assert_called_once()
        else:
            proc = self.process()
            proc.stdout.read.side_effect = [b"partial", OSError("pipe read failed")]
            with patch("konden.execution.local.bash.shutil.which", return_value="bash"), \
                 patch("konden.execution.local.bash.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
                result = await BashCommandRunner(executable="bash").run("x", Path.cwd(), 10)
        self.assertEqual(result.stdout.content, "partial")
        self.assertIn("stdout: OSError", result.io_errors[0])

    async def test_powershell_internal_cleanup_error_passes_through(self):
        proc = self.process()
        error = OSError("invalid job handle")
        job = Mock(close=Mock(side_effect=error))
        with patch("konden.execution.local.powershell.shutil.which", return_value="powershell"), \
             patch("konden.execution.local.powershell.WindowsJob.create", return_value=job), \
             patch("konden.execution.local.powershell.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with self.assertRaises(OSError) as caught:
                await PowerShellCommandRunner(executable="powershell").run("x", Path.cwd(), 10)
        self.assertIs(caught.exception, error)

    async def test_unknown_pipe_error_and_cleanup_error_are_both_preserved(self):
        proc = self.process()
        internal = RuntimeError("stdin bug")
        cleanup = OSError("invalid job handle")
        proc.stdin.drain.side_effect = internal
        job = Mock(close=Mock(side_effect=cleanup))
        with patch("konden.execution.local.powershell.shutil.which", return_value="powershell"), \
             patch("konden.execution.local.powershell.WindowsJob.create", return_value=job), \
             patch("konden.execution.local.powershell.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with self.assertRaises(BaseExceptionGroup) as caught:
                await PowerShellCommandRunner(executable="powershell").run("x", Path.cwd(), 10)
        self.assertEqual(caught.exception.exceptions, (internal, cleanup))
