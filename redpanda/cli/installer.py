"""manifest 安装源：登记本机已安装/手工安装的 CLI。

manifest 源不下载任何二进制——注册 = 解析 resolved_path + 体检 + 登记事实。
产品管理命令在 spawn 前刷新持久化安装 PATH，因此路径解析能看到新装 CLI。
登记后普通调用使用 resolved_path，不改写工作区命令的启动 PATH。
"""

from __future__ import annotations

import os
from pathlib import Path

from redpanda.cli.errors import CliInputError, CliSourceError
from redpanda.sandbox.command import EnvironmentCommandExecutor


class ManifestCliInstaller:
    def __init__(
        self,
        executor: EnvironmentCommandExecutor,
        *,
        cwd: Path,
        timeout_seconds: int = 15,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("解析超时必须大于 0")
        self._executor = executor
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds

    async def resolve_path(self, locator: str) -> str:
        """把 locator 解析为可执行文件绝对路径。

        locator 含路径分隔符或盘符时视为显式路径，必须是已存在的文件；
        否则视为命令名，用 where.exe / command -v 在最新 PATH 中解析。
        """
        if _looks_like_path(locator):
            candidate = Path(locator)
            if not candidate.is_file():
                raise CliInputError(
                    f"显式路径不存在或不是文件: {locator}"
                )
            return str(candidate.resolve())
        command = (
            f"where.exe {locator}"
            if os.name == "nt"
            else f"command -v {locator}"
        )
        result = await self._executor.run(
            command,
            self._cwd,
            self._timeout_seconds,
        )
        if result.timed_out or result.exit_code != 0 or result.io_errors:
            raise CliSourceError(
                f"命令不在 PATH 中: {locator}。"
                "请先安装该 CLI，或以显式可执行文件路径作为 locator。"
            )
        first_line = result.stdout.content.strip().splitlines()[0].strip()
        if not first_line:
            raise CliSourceError(f"无法解析命令路径: {locator}")
        return first_line


def _looks_like_path(locator: str) -> bool:
    return (
        "/" in locator
        or "\\" in locator
        or (len(locator) >= 2 and locator[1] == ":")
    )
