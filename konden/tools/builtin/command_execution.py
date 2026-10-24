from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from konden.filesystem import FileAccess
from konden.execution.api import CommandEnvironment
from konden.tools.results import environment_error
from konden.execution.command import (
    CommandResult,
    CommandStartError,
    ShellNotFoundError,
)
from konden.filesystem.workspace import EnvironmentInputError
from konden.tools.spec import PydanticParameters, ToolSpec

_LOGGER = logging.getLogger(__name__)


COMMAND_DESCRIPTION = """
用途：用 {shell_name} 执行非交互命令与脚本，包括安装、构建、测试、格式化、静态检查和 Git；文件查找、读取和修改用专用文件工具。
文件：直接作用于工作区真实目录，不提供文件回退。命令可以访问工作区之外和网络，执行前需要用户授权；执行后根据实际结果核对影响。
限制：不支持中途的 UAC、sudo 密码或硬件确认；遇到时停止，告诉用户需要在工具外完成的步骤。
执行环境：每次调用独立，不继承上次的环境激活、变量或目录；需要特定环境就在 command 里显式调用解释器或环境管理器。陌生 CLI 或遇到 unknown option 时逐层查 `--help`，不要猜 flag。
{background_behavior}
结果：退出码 0 为 COMMAND_COMPLETED，非 0 为 COMMAND_EXIT_NONZERO（已运行但不算成功）。检查 stdout、stderr 的 truncated 和 io_errors（非空表示输出可能不完整）。
""".strip()


class ForegroundCommandInput(BaseModel):
    cwd: str | None = Field(
        default=None,
        description="命令启动目录；省略时为工作区",
    )
    command: str = Field(description="要交给 Shell 执行的完整命令字符串")


class CommandInput(ForegroundCommandInput):
    wait_seconds: int = Field(
        default=30,
        ge=0,
        le=30,
        description="前台等待秒数，范围 0 到 30；0 立即转后台，不是命令总时限",
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

    async def execute_command(raw: ForegroundCommandInput) -> dict[str, Any]:
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
            wait_seconds = raw.wait_seconds if commands.background_supported else None
            if interrupt is None:
                result = await runner.run(raw.command, cwd, wait_seconds)
            else:
                result = await runner.run(
                    raw.command,
                    cwd,
                    wait_seconds,
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

        return command_result(result, resolved_cwd.workspace_membership.display_path)

    return ToolSpec(
        name="execute_command",
        description=COMMAND_DESCRIPTION.format(shell_name=commands.shell_name, background_behavior=(
            "长命令：超过 wait_seconds 自动转后台继续执行，完成后会通知你；构建、测试、安装或服务可用 wait_seconds=0。"
            "收到 COMMAND_BACKGROUND 后可结束本轮，不要循环查询或用 sleep 等待。需要检查输出时用 read_command，终止时用 stop_command。"
            if commands.background_supported else "当前环境不支持后台命令；调用会在前台等待命令结束。"
        )),
        parameters=PydanticParameters(CommandInput if commands.background_supported else ForegroundCommandInput),
        handler=execute_command,
        requires_authorization=True,
    )


def command_result(result: CommandResult, cwd: str) -> dict[str, Any]:
    data = {
        "cwd": cwd,
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
    if result.background is not None:
        return {
            "ok": True, "code": "COMMAND_BACKGROUND",
            "data": {**data, "background": result.background},
            "hint": "命令仍在后台执行，结束后会通知你。可以继续其他工作或结束本轮，不必等待或循环查询。",
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
