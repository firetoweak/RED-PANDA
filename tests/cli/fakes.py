"""CLI 测试共用的假进程执行器：按命令子串路由预设结果。"""

from __future__ import annotations

from pathlib import Path

from redpanda.sandbox.command import CapturedOutput, CommandResult


def make_result(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    timed_out: bool = False,
) -> CommandResult:
    return CommandResult(
        exit_code=None if timed_out else exit_code,
        stdout=CapturedOutput(stdout, len(stdout), False, 0),
        stderr=CapturedOutput(stderr, len(stderr), False, 0),
        duration_ms=1,
        timed_out=timed_out,
    )


class FakeExecutor:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.routes: dict[str, CommandResult] = {}

    def route(self, needle: str, result: CommandResult) -> None:
        self.routes[needle] = result

    async def run(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: int,
    ) -> CommandResult:
        self.commands.append(command)
        for needle, result in self.routes.items():
            if needle in command:
                return result
        return make_result(exit_code=1)
