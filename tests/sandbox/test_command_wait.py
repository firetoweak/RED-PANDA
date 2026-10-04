from __future__ import annotations

import asyncio
import unittest

from redpanda.sandbox.command import wait_process


class _Process:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self._exit = asyncio.Event()

    async def wait(self) -> int | None:
        await self._exit.wait()
        return self.returncode

    def finish(self, code: int) -> None:
        self.returncode = code
        self._exit.set()


class CommandWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_is_reported_while_the_process_is_still_running(self):
        proc = _Process()
        interrupt = asyncio.Event()
        waiting = asyncio.create_task(wait_process(proc, 5, interrupt))
        await asyncio.sleep(0)
        interrupt.set()

        self.assertEqual(await waiting, "interrupted")

    async def test_known_exit_wins_when_interrupt_is_also_set(self):
        proc = _Process()
        proc.finish(0)
        interrupt = asyncio.Event()
        interrupt.set()

        self.assertEqual(await wait_process(proc, 5, interrupt), "exited")

    async def test_timeout_does_not_report_interrupt(self):
        proc = _Process()

        self.assertEqual(await wait_process(proc, 0.01, None), "timed_out")
