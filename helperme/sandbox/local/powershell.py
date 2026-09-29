from __future__ import annotations

import codecs
import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Mapping, Sequence

from helperme.sandbox.command import (
    BoundedTextCapture,
    CaptureLimit,
    CommandResult,
    CommandStartError,
    ShellNotFoundError,
    wait_process,
)
from helperme.sandbox.local.child_env import (
    CHILD_ENV_OVERLAY,
    latest_persistent_path,
    with_runtime_python_environment,
)
from helperme.sandbox.local.windows_job import WindowsJob


DEFAULT_ENV_NAMES = (
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PSMODULEPATH",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)


class CommandEnvironmentPolicy:
    """从宿主环境中选择明确允许传给命令子进程的变量。"""

    def __init__(
        self,
        forward_names: Sequence[str] = (),
        fixed_values: Mapping[str, str] | None = None,
    ) -> None:
        names = (*DEFAULT_ENV_NAMES, *forward_names)
        if any(not name or not name.strip() for name in names):
            raise ValueError("环境变量名称不能为空")
        self._forward_names = tuple(dict.fromkeys(name.casefold() for name in names))
        self._fixed_values = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            **CHILD_ENV_OVERLAY,
            **dict({} if fixed_values is None else fixed_values),
        }

    def build(self, host_env: Mapping[str, str]) -> dict[str, str]:
        indexed = {name.casefold(): (name, value) for name, value in host_env.items()}
        child_env = {
            indexed[name][0]: indexed[name][1]
            for name in self._forward_names
            if name in indexed
        }
        # PATH 整体替换为最新持久化值：host_env 里的是 Worker 启动快照，
        # 安装器写入注册表的新条目必须在 spawn 前重新合成。是替换不是追加，
        # 否则 CLI 升级换安装位置后会被旧路径遮蔽。
        fresh_path = latest_persistent_path()
        if fresh_path is not None and "path" in self._forward_names:
            key = indexed["path"][0] if "path" in indexed else "PATH"
            child_env[key] = fresh_path
        child_env.update(self._fixed_values)
        return child_env


class PowerShellCommandRunner:
    """在固定 PowerShell 中执行单次前台、非交互命令。"""

    def __init__(
        self,
        executable: str | None = None,
        environment_policy: CommandEnvironmentPolicy | None = None,
        capture_limit: CaptureLimit | None = None,
    ) -> None:
        if executable is not None and not executable.strip():
            raise ValueError("PowerShell executable 不能为空")
        if executable is None:
            for candidate in ("pwsh.exe", "powershell.exe"):
                executable = shutil.which(candidate)
                if executable is not None:
                    break
            else:
                raise ShellNotFoundError(
                    "powershell",
                    "pwsh.exe / powershell.exe",
                )
        self.executable = executable
        self.environment_policy = (
            CommandEnvironmentPolicy()
            if environment_policy is None
            else environment_policy
        )
        self.capture_limit = (
            CaptureLimit() if capture_limit is None else capture_limit
        )

    async def run(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: int,
        *,
        interrupt: asyncio.Event | None = None,
    ) -> CommandResult:
        executable = shutil.which(self.executable)
        if executable is None:
            raise ShellNotFoundError("powershell", self.executable)

        child_env = with_runtime_python_environment(
            self.environment_policy.build(os.environ)
        )
        utf8_command = (
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
            "$OutputEncoding=[Console]::OutputEncoding;"
            + command
        )
        try:
            job = WindowsJob.create()
        except OSError as exc:
            raise CommandStartError(str(exc)) from exc
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$null=[Console]::In.Read();" + utf8_command,
                cwd=cwd,
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            job.close()
            raise CommandStartError(str(exc)) from exc
        except BaseException:
            job.close()
            raise
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None

        try:
            job.assign(proc.pid)
        except OSError as exc:
            proc.kill()
            await proc.wait()
            job.close()
            raise CommandStartError(str(exc)) from exc

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
            job.close()
            await proc.wait()
            await asyncio.gather(*readers)

        timed_out = False
        interrupted = False
        kind = "stdin_failed"
        try:
            try:
                proc.stdin.write(b"\n")
                await proc.stdin.drain()
                proc.stdin.close()
            except OSError as exc:
                io_errors.append(f"stdin: {type(exc).__name__}: {exc}")
            else:
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
