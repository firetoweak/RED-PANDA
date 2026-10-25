from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Mapping, Sequence

from konden.execution.command import (
    CaptureLimit,
    CommandResult,
    ShellNotFoundError,
)
from konden.execution.local.child_env import (
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

    def prepare(self, command: str) -> tuple[list[str], dict[str, str]]:
        executable = shutil.which(self.executable, path=self._lookup_path())
        if executable is None:
            raise ShellNotFoundError("bash", self.executable)
        child_env = self.environment_policy.build(
            os.environ if self._environment is None else self._environment
        )
        return [executable, "--noprofile", "--norc", "-c", command], child_env

    async def run(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: int | None,
        *,
        interrupt: asyncio.Event | None = None,
    ) -> CommandResult:
        from konden.execution.local.owned_command import run_owned_command

        argv, child_env = self.prepare(command)
        return await run_owned_command(argv, child_env, cwd, timeout_seconds, interrupt, self.capture_limit)
