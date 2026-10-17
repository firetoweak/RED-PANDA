"""manifest 安装源：登记本机已安装/手工安装的 CLI。

manifest 源不下载任何二进制——注册 = 解析 resolved_path + 体检 + 登记事实。
命令名在最新持久化 PATH 中解析，因此无需重启即可看到新装 CLI。
登记后普通调用使用 resolved_path，不改写工作区命令的启动 PATH。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from redpanda.cli.errors import CliInputError, CliSourceError
from redpanda.execution.local.child_env import latest_persistent_path


def resolve_cli_path(locator: str) -> str:
    """把 locator 解析为可执行文件绝对路径。

    locator 含路径分隔符或盘符时视为显式路径，必须是已存在的文件；
    否则视为命令名，在最新 PATH 中查找（Windows 读持久化 PATH，其余平台用进程 PATH）。
    """
    if _looks_like_path(locator):
        candidate = Path(locator)
        if not candidate.is_file():
            raise CliInputError(
                f"显式路径不存在或不是文件: {locator}"
            )
        return str(candidate.resolve())
    found = shutil.which(locator, path=latest_persistent_path())
    if found is None:
        raise CliSourceError(
            f"命令不在 PATH 中: {locator}。"
            "请先安装该 CLI，或以显式可执行文件路径作为 locator。"
        )
    return found


def _looks_like_path(locator: str) -> bool:
    return (
        "/" in locator
        or "\\" in locator
        or (len(locator) >= 2 and locator[1] == ":")
    )
