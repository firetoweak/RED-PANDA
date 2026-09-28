from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from helperme.sandbox.api import (
    EnvironmentBinding,
    environment_error,
)
from helperme.sandbox.command import (
    CommandResult,
    CommandStartError,
    ShellNotFoundError,
)
from helperme.sandbox.workspace import EnvironmentInputError
from helperme.tools.builtin.exec_policy import command_requires_authorization
from helperme.tools.spec import PydanticParameters, ToolSpec


EXECUTE_COMMAND_DESCRIPTION = """
用途：使用 {shell_name} 在本机执行 CLI 命令。
何时使用：用于依赖安装、构建、测试、格式化、静态检查、Git、包管理器和运行脚本；常规文件发现、搜索、读取和修改应使用专用文件工具。
关键限制：相对 cwd 从工作区开始，绝对 cwd 按本机路径规则；cwd 只决定启动目录，命令本身不受工作区限制，可能改动本机任何位置的文件；command 使用 {shell_name} 语义；Shell 路径为 {shell_path}；workspace_effect 必须按预期副作用声明；仅支持有超时的前台非交互命令。
CLI 发现：对陌生 CLI 或遇到 unknown option 时，先执行当前层级的 `<cli> --help`（如 `<cli> <子命令> --help`）逐层现查，不要继续猜 flag。
失败/截断后：检查 exit_code、stdout、stderr、timed_out、io_errors 和各流的 truncated；io_errors 非空表示管道失败，采集结果可能不完整；超时或失败时不能假定命令成功，也不要无条件重试可能产生副作用的命令；命令产生的文件变化需通过文件工具或 Git diff 重新验证。
被用户打断时 code 为 COMMAND_INTERRUPTED，ok 为空，已捕获的 stdout/stderr 只是证据（output_is_result 为 false），执行结果未知，不要按失败重试，也不会自动撤销已经发生的副作用。
""".strip()


class ExecuteCommandInput(BaseModel):
    cwd: str | None = Field(
        default=None,
        description="命令启动目录；省略时为工作区，相对路径也从工作区开始",
    )
    command: str = Field(description="要交给 Shell 执行的完整命令字符串")
    workspace_effect: Literal["read_only", "may_write"] = Field(
        default="may_write",
        description=(
            "命令对工作区文件的预期影响；明确只读查询使用 read_only，"
            "可能写入或无法确定时使用 may_write"
        ),
    )
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
    binding: EnvironmentBinding,
    interrupt_source: Callable[[], asyncio.Event | None] = lambda: None,
) -> ToolSpec:
    """`interrupt_source` 由调用方显式提供，默认不可打断。"""

    runner = binding.execution_attachment.command_executor
    async def execute_command(raw: ExecuteCommandInput) -> dict[str, Any]:
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
            )
        except EnvironmentInputError as exc:
            return environment_error(exc)
        cwd = resolved_cwd.native_path

        if not cwd.exists():
            return {
                "ok": False,
                "code": "CWD_NOT_FOUND",
                "error": f"工作目录不存在: {raw.cwd}",
                "execution_location": resolved_cwd.location.to_dict(),
                "workspace_membership": (
                    resolved_cwd.workspace_membership.to_dict()
                ),
            }
        if not cwd.is_dir():
            return {
                "ok": False,
                "code": "CWD_NOT_A_DIRECTORY",
                "error": f"工作目录不是目录: {raw.cwd}",
                "execution_location": resolved_cwd.location.to_dict(),
                "workspace_membership": (
                    resolved_cwd.workspace_membership.to_dict()
                ),
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
                "execution_location": resolved_cwd.location.to_dict(),
                "workspace_membership": (
                    resolved_cwd.workspace_membership.to_dict()
                ),
            }
        except CommandStartError as exc:
            return {
                "ok": False,
                "code": "COMMAND_START_FAILED",
                "error": str(exc),
                "execution_location": resolved_cwd.location.to_dict(),
                "workspace_membership": (
                    resolved_cwd.workspace_membership.to_dict()
                ),
            }

        data = {
            "execution_location": resolved_cwd.location.to_dict(),
            "workspace_membership": (
                resolved_cwd.workspace_membership.to_dict()
            ),
            "cwd": resolved_cwd.workspace_membership.display_path,
            "workspace_effect": raw.workspace_effect,
            **_result_data(result),
        }
        if result.interrupted:
            return {
                "ok": None,
                "code": COMMAND_INTERRUPTED,
                "data": {**data, "output_is_result": False},
                "error": "命令已被打断，执行结果未知。",
                "hint": (
                    "已捕获的 stdout/stderr 只是打断前的输出证据，不构成命令结果。"
                    "外部副作用可能已经发生，不会自动撤销。"
                ),
            }
        if result.timed_out:
            return {
                "ok": False,
                "code": "COMMAND_TIMEOUT",
                "data": data,
                "error": f"命令执行超过 {raw.timeout_seconds} 秒",
            }
        if result.io_errors:
            return {
                "ok": False,
                "code": "COMMAND_IO_FAILED",
                "data": data,
                "error": "命令管道 I/O 失败，采集结果可能不完整",
                "hint": "进程已经启动，可能产生副作用；根据现有输出和实际文件状态决定下一步。",
            }
        return {
            "ok": True,
            "code": "COMMAND_COMPLETED",
            "data": data,
        }

    return ToolSpec(
        name="execute_command",
        description=EXECUTE_COMMAND_DESCRIPTION.format(
            shell_name=binding.shell_name,
            shell_path=binding.shell_path,
        ),
        parameters=PydanticParameters(ExecuteCommandInput),
        handler=execute_command,
        requires_authorization=command_requires_authorization,
    )
