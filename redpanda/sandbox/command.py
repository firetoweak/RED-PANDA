from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


# 终端控制序列与有害控制字符。命令输出是 untrusted external data：
# ANSI/OSC 可伪造界面、隐藏文本，进模型上下文前统一剥除。
_TERMINAL_CONTROL_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC：BEL 或 ST 终止
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[()][0-2]"  # 字符集选择
    r"|\x1b[@-Z\\-_]"  # 两字符转义
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # C0 控制字符（保留 \t \n \r）与 DEL
)


def strip_terminal_control(text: str) -> str:
    return _TERMINAL_CONTROL_RE.sub("", text)


@dataclass(frozen=True)
class CaptureLimit:
    max_chars: int = 6_000
    head_chars: int = 2_400

    def __post_init__(self) -> None:
        if type(self.max_chars) is not int or self.max_chars <= 0:
            raise ValueError("max_chars 必须大于 0")
        if (
            type(self.head_chars) is not int
            or not 0 <= self.head_chars <= self.max_chars
        ):
            raise ValueError("head_chars 必须位于 0 到 max_chars 之间")


@dataclass(frozen=True)
class CapturedOutput:
    content: str
    total_chars: int
    truncated: bool
    omitted_chars: int

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "content": self.content,
            "total_chars": self.total_chars,
            "truncated": self.truncated,
            "omitted_chars": self.omitted_chars,
        }


class BoundedTextCapture:
    """Incrementally retain bounded head/tail text for command output."""

    def __init__(self, limit: CaptureLimit) -> None:
        self._limit = limit
        self._head = ""
        self._tail = ""
        self._total_chars = 0

    def feed(self, text: str) -> None:
        text = strip_terminal_control(text)
        self._total_chars += len(text)
        remaining_head = self._limit.head_chars - len(self._head)
        if remaining_head > 0:
            self._head += text[:remaining_head]
            text = text[remaining_head:]

        tail_chars = self._limit.max_chars - self._limit.head_chars
        if tail_chars > 0 and text:
            self._tail = (self._tail + text)[-tail_chars:]

    def finish(self) -> CapturedOutput:
        truncated = self._total_chars > self._limit.max_chars
        omitted_chars = max(0, self._total_chars - self._limit.max_chars)
        if truncated:
            marker = f"\n... [截断 {omitted_chars} 字符] ...\n"
            content = self._head + marker + self._tail
        else:
            content = self._head + self._tail
        return CapturedOutput(
            content=content,
            total_chars=self._total_chars,
            truncated=truncated,
            omitted_chars=omitted_chars,
        )


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    stdout: CapturedOutput
    stderr: CapturedOutput
    duration_ms: int
    timed_out: bool
    io_errors: tuple[str, ...] = ()
    interrupted: bool = False


class ShellNotFoundError(FileNotFoundError):
    def __init__(self, shell_name: str, executable: str) -> None:
        self.shell_name = shell_name
        self.executable = executable
        super().__init__(
            f"未找到 Shell 可执行程序: {shell_name} ({executable})"
        )


class CommandStartError(OSError):
    pass


class EnvironmentCommandExecutor(Protocol):
    async def run(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: int,
        *,
        interrupt: asyncio.Event | None = None,
    ) -> CommandResult:
        ...


async def wait_process(
    proc: asyncio.subprocess.Process,
    timeout_seconds: float,
    interrupt: asyncio.Event | None,
) -> str:
    """等待进程退出、超时，或打断。进程已经退出时，已知结果优先于打断。"""

    exit_task = asyncio.create_task(proc.wait())
    interrupt_task = (
        asyncio.create_task(interrupt.wait()) if interrupt is not None else None
    )
    try:
        waiters = {exit_task}
        if interrupt_task is not None:
            waiters.add(interrupt_task)
        done, _pending = await asyncio.wait(
            waiters,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        cleanup = asyncio.create_task(_consume_waits(exit_task, interrupt_task))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
        raise
    if proc.returncode is not None or exit_task in done:
        await _consume_waits(exit_task, interrupt_task)
        return "exited"
    if interrupt_task is not None and interrupt_task in done:
        await _consume_waits(exit_task, interrupt_task)
        return "interrupted"
    await _consume_waits(exit_task, interrupt_task)
    return "timed_out"


async def _consume_waits(*tasks: asyncio.Task[object] | None) -> None:
    for task in tasks:
        if task is None:
            continue
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            continue
