"""CLI 体检：检查 agent-friendly 约定，只记录事实，不设准入门槛。

体检用 resolved_path 绝对路径跑，不依赖 PATH——安装后当前 Worker 的
PATH 快照尚未刷新时体检依然有效。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from redpanda.cli.models import CliHealth, utc_now
from redpanda.sandbox.command import CommandResult, EnvironmentCommandExecutor

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?")


@dataclass(frozen=True)
class CliProbe:
    version: str | None
    health: CliHealth


def executable_invocation(executable_path: str) -> str:
    """把绝对路径变成当前 shell 的可调用前缀（PowerShell 需要 & 调用符）。"""
    quoted = f'"{executable_path}"'
    return f"& {quoted}" if os.name == "nt" else quoted


def parse_version(text: str) -> str | None:
    match = _VERSION_RE.search(text)
    return None if match is None else match.group(0)


class CliHealthChecker:
    def __init__(
        self,
        executor: EnvironmentCommandExecutor,
        *,
        cwd: Path,
        timeout_seconds: int = 15,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("体检超时必须大于 0")
        self._executor = executor
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds

    async def probe(self, resolved_path: str) -> CliProbe:
        """跑 --help 与 --version，返回解析出的版本与体检事实。"""
        invocation = executable_invocation(resolved_path)
        help_result = await self._executor.run(
            f"{invocation} --help",
            self._cwd,
            self._timeout_seconds,
        )
        version_result = await self._executor.run(
            f"{invocation} --version",
            self._cwd,
            self._timeout_seconds,
        )
        help_text = help_result.stdout.content + "\n" + help_result.stderr.content
        version_text = (
            version_result.stdout.content + "\n" + version_result.stderr.content
        )
        help_ok = _completed(help_result) and bool(help_text.strip())
        version_ok = _completed(version_result) and bool(version_text.strip())
        lowered = help_text.casefold()
        return CliProbe(
            version=parse_version(version_text) if version_ok else None,
            health=CliHealth(
                help_ok=help_ok,
                version_ok=version_ok,
                help_mentions_json=(
                    "--json" in lowered or "--format json" in lowered
                ),
                checked_at=utc_now(),
            ),
        )


def _completed(result: CommandResult) -> bool:
    return (
        not result.timed_out
        and result.exit_code == 0
        and not result.io_errors
    )
