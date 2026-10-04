"""正在执行的 execute_command。打断只作用于这些进程，不是通用工具取消。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from redpanda.tools.builtin.command_execution import COMMAND_INTERRUPTED


_current: ContextVar[asyncio.Event | None] = ContextVar(
    "command_interrupt",
    default=None,
)


class LiveCommand:
    def __init__(
        self,
        session_id: str,
        command_id: str,
        attempt_id: str,
        event: asyncio.Event,
        future: asyncio.Future[object],
    ) -> None:
        self.session_id = session_id
        self.command_id = command_id
        self.attempt_id = attempt_id
        self.event = event
        self.future = future


class CommandInterrupts:
    def __init__(self) -> None:
        self._live: dict[str, LiveCommand] = {}

    def current(self) -> asyncio.Event | None:
        """交给 execute_command 的显式取值口；`run_interruptible` 之外恒为 None。"""

        return _current.get()

    def arm(
        self,
        *,
        session_id: str,
        command_id: str,
        attempt_id: str,
    ) -> asyncio.Event:
        if attempt_id in self._live:
            raise ValueError(f"command interrupt already armed: {attempt_id}")
        event = asyncio.Event()
        live = LiveCommand(
            session_id,
            command_id,
            attempt_id,
            event,
            asyncio.get_running_loop().create_future(),
        )
        self._live[attempt_id] = live
        return event

    def finish(self, attempt_id: str, result: object) -> None:
        live = self._live.pop(attempt_id)
        live.future.set_result(result)

    def fail(self, attempt_id: str, exc: BaseException) -> None:
        live = self._live.pop(attempt_id)
        live.future.set_exception(exc)

    def signal(self) -> tuple[LiveCommand, ...]:
        lives = tuple(self._live.values())
        for live in lives:
            live.event.set()
        return lives

    async def wait(
        self,
        lives: tuple[LiveCommand, ...],
    ) -> tuple[tuple[tuple[LiveCommand, object], ...], tuple[BaseException, ...]]:
        interrupted: list[tuple[LiveCommand, object]] = []
        errors: list[BaseException] = []
        for live in lives:
            try:
                result = await live.future
            except BaseException as exc:
                errors.append(exc)
                continue
            if type(result) is dict and result["code"] == COMMAND_INTERRUPTED:
                interrupted.append((live, result))
        return tuple(interrupted), tuple(errors)


async def run_interruptible(
    interrupts: CommandInterrupts,
    *,
    session_id: str,
    command_id: str,
    attempt_id: str,
    execute: Callable[[], Awaitable[object]],
) -> object:
    event = interrupts.arm(
        session_id=session_id,
        command_id=command_id,
        attempt_id=attempt_id,
    )
    token = _current.set(event)
    try:
        result = await execute()
    except BaseException as exc:
        interrupts.fail(attempt_id, exc)
        raise
    else:
        interrupts.finish(attempt_id, result)
        return result
    finally:
        _current.reset(token)
