"""Foreground Linux commands reuse the task owner that survives caller death."""
from __future__ import annotations

import asyncio
import codecs
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from konden.execution.command import (
    BoundedTextCapture,
    CommandResult,
    CommandStartError,
    wait_process,
)
from konden.execution.process_linux import Running


class _Foreground:
    def __init__(self, running):
        self.running = running
        self.returncode = None

    async def wait(self):
        while self.returncode is None:
            poll = asyncio.create_task(asyncio.to_thread(self.running.poll, leader_only=True))
            try:
                self.returncode = await asyncio.shield(poll)
            except asyncio.CancelledError:
                await poll
                raise
            if self.returncode is None:
                await asyncio.sleep(.02)
        return self.returncode


async def run_owned_command(argv, environment, cwd, timeout_seconds, interrupt, capture_limit):
    started = time.perf_counter()
    with TemporaryDirectory(prefix="konden-command-") as directory:
        spawn = asyncio.create_task(asyncio.to_thread(Running, argv, cwd, environment, directory))
        try:
            running = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            running = await spawn
            await asyncio.to_thread(running.close)
            raise
        except OSError as exc:
            raise CommandStartError(str(exc)) from exc
        process = _Foreground(running)

        async def close():
            cleanup = asyncio.create_task(asyncio.to_thread(running.close))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

        try:
            kind = await wait_process(process, timeout_seconds, interrupt)
        except BaseException as run_error:
            try:
                await close()
            except BaseException as cleanup_error:
                if cleanup_error is not run_error:
                    raise BaseExceptionGroup("命令执行失败且清理失败", [run_error, cleanup_error]) from None
                raise
            raise
        else:
            await close()

        io_errors = []

        def output(name):
            capture = BoundedTextCapture(capture_limit)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                with (Path(directory) / f"{name}.bin").open("rb") as stream:
                    while chunk := stream.read(4096):
                        capture.feed(decoder.decode(chunk))
            except OSError as exc:
                io_errors.append(f"{name}: {type(exc).__name__}: {exc}")
            capture.feed(decoder.decode(b"", final=True))
            return capture.finish()

        return CommandResult(
            exit_code=process.returncode if kind == "exited" else None,
            stdout=output("stdout"), stderr=output("stderr"),
            duration_ms=round((time.perf_counter() - started) * 1000),
            timed_out=kind == "timed_out", interrupted=kind == "interrupted",
            io_errors=tuple(io_errors),
        )
