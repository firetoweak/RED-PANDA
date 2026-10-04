"""execute_command 的轻量授权策略（ExecPolicy）。

定位：防止误操作的**软边界**，不是安全边界。规则只做命令文本的前缀匹配，
不解析 shell AST；变量、alias、脚本、管道组合都可能绕过它，这是刻意的——
真正的安全边界是 sandbox / 凭据隔离 / 网络隔离，不是这里的清单。

verdict 只有两档：未命中 allow（直接派发），命中 ask（派发前等待用户确认）。
"""

from __future__ import annotations

from collections.abc import Mapping

# 危险前缀清单：覆盖各 shell 的删除、磁盘与电源操作，以及常见 CLI 的不可逆动作。
# 匹配前命令文本会压缩连续空白并 casefold；清单项统一小写、带尾随空格，
# 判定文本尾部补一个空格，保证 "git push -f" 这类裸结尾也能命中。
_ASK_PREFIXES: tuple[str, ...] = (
    # 删除（PowerShell / cmd / POSIX）
    "remove-item ",
    "ri ",
    "rd ",
    "rmdir ",
    "del ",
    "erase ",
    "rm ",
    # 磁盘与系统
    "format ",
    "mkfs ",
    "mkfs.",
    "dd ",
    "diskpart ",
    "shutdown ",
    "restart-computer ",
    "stop-computer ",
    # git 不可逆
    "git push --force ",
    "git push --force-with-lease ",
    "git push -f ",
    "git reset --hard ",
    "git clean -f ",
    # gh 不可逆
    "gh repo delete ",
    "gh repo archive ",
    "gh release delete ",
    "gh secret remove ",
)


def command_requires_authorization(arguments: Mapping[str, object]) -> bool:
    """execute_command 的授权判定：True = ask（派发前等待用户确认）。"""
    command = arguments.get("command")
    if type(command) is not str:
        return False
    normalized = " ".join(command.split()).casefold() + " "
    return any(normalized.startswith(prefix) for prefix in _ASK_PREFIXES)
