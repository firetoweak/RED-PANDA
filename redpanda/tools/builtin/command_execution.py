from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from redpanda.filesystem import FileAccess
from redpanda.execution.api import CommandEnvironment
from redpanda.tools.results import environment_error
from redpanda.execution.command import (
    CommandResult,
    CommandStartError,
    ShellNotFoundError,
)
from redpanda.filesystem.workspace import EnvironmentInputError
from redpanda.tools.spec import PydanticParameters, ToolSpec

_LOGGER = logging.getLogger(__name__)


COMMAND_DESCRIPTION = """
用途：用 {shell_name} 执行一次有超时的前台非交互命令，用于安装、构建、测试、格式化、静态检查、Git 和运行脚本；文件查找、读取和修改用专用文件工具。
文件：直接作用于工作区真实目录，不提供文件回退；在 Git 项目中，尚未提交的改动用 git diff 核对，也要检查暂存区和未跟踪文件，已提交的改动用 git show 或提交差异核对。命令可以访问工作区之外和网络，执行前需要用户授权。
限制：不支持中途的 UAC、sudo 密码或硬件确认；遇到时停止，告诉用户需要在工具外完成的步骤。
执行环境：每次调用独立，不继承上次的环境激活、变量或目录；需要特定环境就在 command 里显式调用解释器或环境管理器。陌生 CLI 或遇到 unknown option 时逐层查 `--help`，不要猜 flag。
结果：退出码 0 为 COMMAND_COMPLETED，非 0 为 COMMAND_EXIT_NONZERO（已运行但不算成功）。检查 stdout、stderr 的 truncated 和 io_errors（非空表示输出可能不完整）。
""".strip()


class CommandInput(BaseModel):
    cwd: str | None = Field(
        default=None,
        description="命令启动目录；省略时为工作区",
    )
    command: str = Field(description="要交给 Shell 执行的完整命令字符串")
    timeout_seconds: int = Field(
        default=60,
        ge=1,
        le=300,
        description="命令超时秒数，范围 1 到 300",
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
) -> ToolSpec:
    """`interrupt_source` 由调用方显式提供，默认不可打断。"""

    async def execute_command(raw: CommandInput) -> dict[str, Any]:
        runner = commands.command_executor
        if not raw.command.strip():
            return {
                "ok": False,
                "code": "EMPTY_COMMAND",
                "error": "command 不能为空",
            }

        try:
            resolved_cwd = binding.resolver.resolve(
                "." if raw.cwd is None else raw.cwd, access="write",
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

        data = {
            "cwd": resolved_cwd.workspace_membership.display_path,
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
        description=COMMAND_DESCRIPTION.format(shell_name=commands.shell_name),
        parameters=PydanticParameters(CommandInput),
        handler=execute_command,
        requires_authorization=True,
    )
