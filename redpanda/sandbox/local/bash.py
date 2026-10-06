from __future__ import annotations

import asyncio
import codecs
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Mapping, Sequence

from redpanda.sandbox.command import (
    BoundedTextCapture,
    CaptureLimit,
    CommandResult,
    CommandStartError,
    ShellNotFoundError,
    wait_process,
)
from redpanda.sandbox.local.child_env import (
    CHILD_ENV_OVERLAY,
)


DEFAULT_ENV_NAMES = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "TMPDIR",
    "USER",
    "VIRTUAL_ENV",
)


class BashCommandEnvironmentPolicy:
    """Select explicitly allowed host variables for Bash child processes."""

    def __init__(
        self,
        forward_names: Sequence[str] = (),
        fixed_values: Mapping[str, str] | None = None,
    ) -> None:
        names = (*DEFAULT_ENV_NAMES, *forward_names)
        if any(not name or not name.strip() for name in names):
            raise ValueError("环境变量名称不能为空")
        self._forward_names = tuple(dict.fromkeys(names))
        self._fixed_values = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            **CHILD_ENV_OVERLAY,
            **dict({} if fixed_values is None else fixed_values),
        }

    def build(self, host_env: Mapping[str, str]) -> dict[str, str]:
        child_env = {
            name: host_env[name]
            for name in self._forward_names
            if name in host_env
        }
        child_env.update(self._fixed_values)
        return child_env


class BashCommandRunner:
    """Run one foreground, non-interactive command in a fixed Bash."""

    def __init__(
        self,
        executable: str | None = None,
        environment_policy: BashCommandEnvironmentPolicy | None = None,
        capture_limit: CaptureLimit | None = None,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._environment = None if environment is None else dict(environment)
        if executable is not None and not executable.strip():
            raise ValueError("Bash executable 不能为空")
        if executable is None:
            executable = shutil.which("bash", path=self._lookup_path())
            if executable is None:
                raise ShellNotFoundError("bash", "bash")
        self.executable = executable
        self.environment_policy = (
            BashCommandEnvironmentPolicy()
            if environment_policy is None
            else environment_policy
        )
        self.capture_limit = (
            CaptureLimit() if capture_limit is None else capture_limit
        )

    def _lookup_path(self) -> str:
        source = os.environ if self._environment is None else self._environment
        return source.get("PATH", os.defpath)

    async def run(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: int,
        *,
        interrupt: asyncio.Event | None = None,
    ) -> CommandResult:
        executable = shutil.which(self.executable, path=self._lookup_path())
        if executable is None:
            raise ShellNotFoundError("bash", self.executable)

        child_env = self.environment_policy.build(
            os.environ if self._environment is None else self._environment
        )
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                executable,
                "--noprofile",
                "--norc",
                "-c",
                command,
                cwd=cwd,
                env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise CommandStartError(str(exc)) from exc
        assert proc.stdout is not None
        assert proc.stderr is not None

        stdout_capture = BoundedTextCapture(self.capture_limit)
        stderr_capture = BoundedTextCapture(self.capture_limit)
        io_errors: list[str] = []

        async def drain(stream, capture: BoundedTextCapture, name: str) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                try:
                    chunk = await stream.read(4_096)
                except OSError as exc:
                    io_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                    break
                if not chunk:
                    break
                capture.feed(decoder.decode(chunk))
            capture.feed(decoder.decode(b"", final=True))

        readers = (
            asyncio.create_task(drain(proc.stdout, stdout_capture, "stdout")),
            asyncio.create_task(drain(proc.stderr, stderr_capture, "stderr")),
        )

        async def terminate_and_drain() -> None:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            await asyncio.gather(*readers)

        timed_out = False
        interrupted = False
        try:
            kind = await wait_process(proc, timeout_seconds, interrupt)
        except BaseException as run_error:
            cleanup = asyncio.create_task(terminate_and_drain())
            try:
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
            except BaseException as cleanup_error:
                if cleanup_error is not run_error:
                    raise BaseExceptionGroup(
                        "命令执行失败且清理失败", [run_error, cleanup_error]
                    )
                raise
            raise
        else:
            if kind == "exited":
                await asyncio.gather(*readers)
            else:
                timed_out = kind == "timed_out"
                interrupted = kind == "interrupted"
                await terminate_and_drain()

        return CommandResult(
            exit_code=None if timed_out or interrupted else proc.returncode,
            stdout=stdout_capture.finish(),
            stderr=stderr_capture.finish(),
            duration_ms=round((time.perf_counter() - started) * 1_000),
            timed_out=timed_out,
            io_errors=tuple(io_errors),
            interrupted=interrupted,
        )
