"""CLI 领域错误。"""

from __future__ import annotations


class CliError(Exception):
    """CLI 领域错误基类。"""


class CliAlreadyInstalledError(CliError):
    """CLI 已登记。"""


class CliNotFoundError(CliError):
    """CLI 未登记。"""


class CliInputError(CliError):
    """CLI 输入或登记状态不符合契约。"""


class CliSourceError(CliError):
    """CLI 来源解析失败（命令不在 PATH、显式路径无效等）。"""
