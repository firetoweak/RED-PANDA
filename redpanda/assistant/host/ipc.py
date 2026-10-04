"""Private, inherited process pipe. Application messages only."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from inspect import isawaitable
import traceback
from uuid import uuid4


@dataclass(frozen=True)
class ProcessFailure:
    exception_type: str
    message: str
    traceback: str

    @classmethod
    def capture(cls, error: BaseException) -> ProcessFailure:
        return cls(
            f"{type(error).__module__}.{type(error).__qualname__}",
            str(error),
            "".join(traceback.format_exception(error)),
        )

    def render(self) -> str:
        return f"{self.exception_type}: {self.message}\n{self.traceback}"


class WorkerFailed(RuntimeError):
    def __init__(self, session_id: str, failure: ProcessFailure) -> None:
        self.session_id = session_id
        self.failure = failure
        super().__init__(
            f"{session_id}: {failure.exception_type}: {failure.message}\n{failure.traceback}"
        )


class PipePeer:
    def __init__(self, connection, handler, signal, *, peer_alive=None) -> None:
        self.connection = connection
        self.peer_alive = peer_alive
        self.handler = handler
        self.signal = signal
        self.pending: dict[str, asyncio.Future] = {}
        self.delta_sinks: dict[str, object] = {}
        self.reasoning_delta_sinks: dict[str, object] = {}
        self.inflight: dict[str, asyncio.Task] = {}
        self.tasks: set[asyncio.Task] = set()
        self.stopped = False
        self.failure: BaseException | None = None
        self.send_lock = asyncio.Lock()
        self.active_request_id: str | None = None

    async def send(self, message) -> None:
        async with self.send_lock:
            await asyncio.to_thread(self.connection.send, message)

    async def request(
        self,
        operation: str,
        session_id: str,
        arguments: dict,
        *,
        on_delta=None,
        on_reasoning_delta=None,
    ):
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        if on_delta is not None:
            self.delta_sinks[request_id] = on_delta
        if on_reasoning_delta is not None:
            self.reasoning_delta_sinks[request_id] = on_reasoning_delta
        try:
            await self.send(("request", request_id, operation, session_id, arguments))
            return await future
        except asyncio.CancelledError:
            await asyncio.shield(self.send(("abort", request_id)))
            raise
        finally:
            del self.pending[request_id]
            self.delta_sinks.pop(request_id, None)
            self.reasoning_delta_sinks.pop(request_id, None)

    async def _handle(self, request_id, operation, session_id, arguments):
        self.active_request_id = request_id
        try:
            result = await self.handler(operation, session_id, arguments)
            await self.send(("response", request_id, result))
        except asyncio.CancelledError:
            return
        finally:
            if self.active_request_id == request_id:
                self.active_request_id = None

    def _done(self, task, request_id=None):
        self.tasks.discard(task)
        if request_id is not None:
            self.inflight.pop(request_id, None)
        if not task.cancelled() and task.exception() is not None:
            self.failure = task.exception()

    def _receive(self):
        if self.connection.poll(0.1):
            return self.connection.recv()
        # A process killed during spawn may leave an unclaimed duplicated pipe
        # handle in the parent. Drain buffered messages, then use process death
        # as EOF rather than waiting forever for that duplicate to close.
        if self.peer_alive is not None and not self.peer_alive():
            raise EOFError("peer process exited")
        return None

    async def run(self):
        while not self.stopped:
            if self.failure is not None:
                raise self.failure
            message = await asyncio.to_thread(self._receive)
            if message is None:
                continue
            kind, *payload = message
            if kind == "request":
                request_id = payload[0]
                task = asyncio.create_task(self._handle(*payload))
                self.inflight[request_id] = task
                self.tasks.add(task)
                task.add_done_callback(
                    lambda done, request_id=request_id: self._done(done, request_id)
                )
            elif kind == "abort":
                (request_id,) = payload
                task = self.inflight.get(request_id)
                if task is not None:
                    task.cancel()
            elif kind == "delta":
                request_id, text = payload
                sink = self.delta_sinks.get(request_id)
                if sink is not None:
                    emitted = sink(text)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "reasoning_delta":
                request_id, text = payload
                sink = self.reasoning_delta_sinks.get(request_id)
                if sink is not None:
                    emitted = sink(text)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "response":
                request_id, result = payload
                future = self.pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(result)
            else:
                await self.signal(kind, *payload)

    async def close(self, error: BaseException):
        self.stopped = True
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
