from __future__ import annotations

import asyncio
import os
import logging
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from redpanda.filesystem import FileAccess
from redpanda.execution.api import CommandEnvironment
from redpanda.tools.results import environment_error
from redpanda.execution.command import (
    CommandResult,
    CommandConstraintError,
    CommandStartError,
    ShellNotFoundError,
)
from redpanda.filesystem.workspace import EnvironmentInputError
from redpanda.tools.builtin.exec_policy import command_requires_authorization
from redpanda.tools.spec import PydanticParameters, ToolSpec

_LOGGER = logging.getLogger(__name__)


EXECUTE_COMMAND_DESCRIPTION = """
用途：用 {shell_name} 执行一次有超时的前台非交互命令，用于安装、构建、测试、格式化、静态检查、Git 和运行脚本；文件查找、读取和修改用专用文件工具。
参数：workspace_effect 按预期副作用声明。file_access 默认 managed；host 直接写本机文件，需授权且不可回退；managed 失败时不要改用 host 重试。{write_constraint}
限制：managed 的 may_write 命令在工作区内不支持改变所有者、设置时间戳、目录重命名、创建硬链接或特殊文件，有多个硬链接的文件不能删除或重命名。不支持中途的 UAC、sudo 密码或硬件确认；遇到时停止，告诉用户需要在工具外完成的步骤。
执行环境：每次调用独立，不继承上次的环境激活、变量或目录；需要特定环境就在 command 里显式调用解释器或环境管理器。陌生 CLI 或遇到 unknown option 时逐层查 `--help`，不要猜 flag。
结果：退出码 0 为 COMMAND_COMPLETED，非 0 为 COMMAND_EXIT_NONZERO（已运行但不算成功）。检查 stdout、stderr 的 truncated 和 io_errors（非空表示输出可能不完整）；命令产生的文件变化用文件工具或 git diff 核对。
""".strip()


DIRECT_COMMAND_DESCRIPTION = """
用途：用 {shell_name} 执行一次有超时的前台非交互命令，用于安装、构建、测试、格式化、静态检查、Git 和运行脚本；文件查找、读取和修改用专用文件工具。
文件：直接作用于工作区真实目录，不提供文件回退；用 git diff 和实际文件核对改动，也要检查未跟踪文件。命令可以访问工作区之外和网络，执行前需要用户授权。
限制：不支持中途的 UAC、sudo 密码或硬件确认；遇到时停止，告诉用户需要在工具外完成的步骤。
执行环境：每次调用独立，不继承上次的环境激活、变量或目录；需要特定环境就在 command 里显式调用解释器或环境管理器。陌生 CLI 或遇到 unknown option 时逐层查 `--help`，不要猜 flag。
结果：退出码 0 为 COMMAND_COMPLETED，非 0 为 COMMAND_EXIT_NONZERO（已运行但不算成功）。检查 stdout、stderr 的 truncated 和 io_errors（非空表示输出可能不完整）。
""".strip()


class DirectCommandInput(BaseModel):
    cwd: str | None = Field(
        default=None,
        description="命令启动目录；省略时为工作区",
    )
    command: str = Field(description="要交给 Shell 执行的完整命令字符串")
    workspace_effect: Literal["read_only", "may_write"] = Field(
        default="may_write",
        description=(
            "命令对工作区文件的影响；只读查询使用 read_only，"
            "可能写入工作区文件（含缓存）或无法确定时使用 may_write"
        ),
    )
    timeout_seconds: int = Field(
        default=60,
        ge=1,
        le=300,
        description="命令超时秒数，范围 1 到 300",
    )


class ExecuteCommandInput(DirectCommandInput):
    file_access: Literal["managed", "host"] = Field(
        default="managed",
        description="managed：工作区内修改可用 get_changes 核对、可回退；host：直接访问本机文件，不可回退。",
    )


COMMAND_INTERRUPTED = "COMMAND_INTERRUPTED"


def _result_data(result: CommandResult) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout.to_dict(),
        "stderr": result.stderr.to_dict(),
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
        "io_errors": list(result.io_errors),
    }


def create_command_execution_spec(
    binding: FileAccess,
    commands: CommandEnvironment,
    interrupt_source: Callable[[], asyncio.Event | None] = lambda: None,
    *,
    direct_workspace: bool = False,
) -> ToolSpec:
    """`interrupt_source` 由调用方显式提供，默认不可打断。"""

    async def execute_command(raw: DirectCommandInput) -> dict[str, Any]:
        managed = not direct_workspace and raw.file_access == "managed"
        runner = commands.command_executor if managed else commands.host_command_executor
        file_access = {} if direct_workspace else {"file_access": raw.file_access}
        if not raw.command.strip():
            return {
                "ok": False,
                "code": "EMPTY_COMMAND",
                "error": "command 不能为空",
            }

        try:
            resolved_cwd = binding.resolver.resolve(
                "." if raw.cwd is None else raw.cwd,
                access=(
                    "write"
                    if raw.workspace_effect == "may_write"
                    else "read"
                ),
                project=managed,
            )
        except EnvironmentInputError as exc:
            return environment_error(exc)
        cwd = resolved_cwd.native_path

        if not cwd.exists():
            return {
                "ok": False,
                "code": "CWD_NOT_FOUND",
                "error": f"工作目录不存在: {raw.cwd}",
            }
        if not cwd.is_dir():
            return {
                "ok": False,
                "code": "CWD_NOT_A_DIRECTORY",
                "error": f"工作目录不是目录: {raw.cwd}",
            }

        try:
            interrupt = interrupt_source()
            if interrupt is None:
                result = await runner.run(raw.command, cwd, raw.timeout_seconds)
            else:
                result = await runner.run(
                    raw.command,
                    cwd,
                    raw.timeout_seconds,
                    interrupt=interrupt,
                )
        except ShellNotFoundError as exc:
            return {
                "ok": False,
                "code": "SHELL_NOT_FOUND",
                "error": str(exc),
                "shell": exc.shell_name,
                "executable": exc.executable,
            }
        except CommandStartError as exc:
            _LOGGER.warning("Command did not start: %s", exc)
            return {
                "ok": False,
                "code": "COMMAND_START_FAILED",
                "error": "命令未能启动；检查 cwd 和命令名后重试一次，仍失败就告诉用户本机命令执行不可用。",
            }
        except CommandConstraintError as exc:
            _LOGGER.warning("Workspace command constraint failed: %s", exc)
            return {
                "ok": None,
                "code": "COMMAND_CONSTRAINT_FAILED",
                "error": (
                    "命令请求了不受支持的进程启动方式；仅支持本机 x64 前台工具。已被停止，可能已部分执行。"
                    if exc.unsupported_process else
                    "命令无法继续满足本机执行限制，已停止，可能已部分执行。"
                ),
                "data": {
                    "cwd": resolved_cwd.workspace_membership.display_path,
                    "workspace_effect": raw.workspace_effect,
                    **file_access,
                    **_result_data(exc.result), "exit_code": None, "output_is_result": False,
                },
                "hint": "先检查相关文件或外部状态，再决定是否调整命令或重试。",
            }

        data = {
            "cwd": resolved_cwd.workspace_membership.display_path,
            "workspace_effect": raw.workspace_effect,
            **file_access,
            **_result_data(result),
        }
        if result.interrupted:
            return {
                "ok": None,
                "code": COMMAND_INTERRUPTED,
                "data": {**data, "output_is_result": False},
                "error": "命令被用户停止，可能已部分执行。",
                "hint": "已捕获的输出不完整；续跑前先检查相关文件和外部状态，避免重复副作用。",
            }
        if result.timed_out:
            return {
                "ok": False,
                "code": "COMMAND_TIMEOUT",
                "data": {**data, "output_is_result": False},
                "error": f"命令执行超过 {raw.timeout_seconds} 秒，已因超时终止，可能已部分执行。",
                "hint": "已捕获的输出不完整；续跑前先检查现场。需要更长时间可调大 timeout_seconds。",
            }
        if result.io_errors:
            return {
                "ok": False,
                "code": "COMMAND_IO_FAILED",
                "data": data,
                "error": "命令管道 I/O 失败，采集结果可能不完整",
                "hint": "进程已经启动，可能产生副作用；根据现有输出和实际文件状态决定下一步。",
            }
        if result.exit_code != 0:
            return {
                "ok": True,
                "code": "COMMAND_EXIT_NONZERO",
                "data": data,
                "hint": "命令已执行但退出码非零；先看 stdout/stderr 判断原因再决定下一步，不要当作成功。",
            }
        return {
            "ok": True,
            "code": "COMMAND_COMPLETED",
            "data": data,
        }

    return ToolSpec(
        name="execute_command",
        description=(DIRECT_COMMAND_DESCRIPTION if direct_workspace else EXECUTE_COMMAND_DESCRIPTION).format(
            shell_name=commands.shell_name,
            write_constraint=(
                "Windows managed 命令写文件时用相对路径或由当前目录推出的绝对路径，不要用工作区的绝对路径写文件；"
                "只支持 x64 前台工具，不支持调试启动或命令结束后继续存活的子进程。持续运行的程序用 start_process。"
                if os.name == "nt" else
                "误标为 read_only 的修改不能回退。"
            ),
        ),
        parameters=PydanticParameters(DirectCommandInput if direct_workspace else ExecuteCommandInput),
        handler=execute_command,
        requires_authorization=True if direct_workspace else command_requires_authorization,
    )
