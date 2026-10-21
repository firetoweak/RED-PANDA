from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from redpanda.assistant.host.ipc import PipePeer, ProcessFailure, WorkerFailed
from redpanda.assistant.host.supervisor import HostSupervisor, Worker


class HostRequestFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_peer_close_does_not_send_abort_back_to_exiting_worker(self):
        host = object.__new__(HostSupervisor)
        host.closed = False
        host.locks = {}
        host._stop_idle = AsyncMock()
        sent = asyncio.Event()
        messages = []
        loop = asyncio.get_running_loop()

        def send(message):
            messages.append(message[0])
            if message[0] == "abort":
                raise BrokenPipeError("Worker has exited")
            loop.call_soon_threadsafe(sent.set)

        peer = PipePeer(SimpleNamespace(send=send), host.request, AsyncMock())
        worker = Worker(None, peer)
        host.workers = {"session": worker}
        handler = asyncio.create_task(peer._handle("incoming", "view", "session", {}))
        peer.tasks.add(handler)
        handler.add_done_callback(peer._done)
        await sent.wait()
        worker.stopping = True
        try:
            async with asyncio.timeout(1):
                await peer.close(RuntimeError("Worker exited"))
        finally:
            worker.exited.set()
            await asyncio.gather(handler, return_exceptions=True)
        self.assertEqual(messages, ["request"])
        self.assertFalse(peer.pending)

    async def test_pipe_close_waits_for_original_failure_without_duplicate_report(self):
        for transport_error in (EOFError(), ConnectionResetError(), BrokenPipeError()):
            with self.subTest(error=type(transport_error).__name__):
                host = object.__new__(HostSupervisor)
                host.closed = False
                host.locks = {}
                host.watchers = set()
                host.failures = asyncio.Queue()
                host.intentionally_stopped = set()
                sent = asyncio.Event()

                async def fail_send(*args):
                    sent.set()
                    raise transport_error

                worker = Worker(None, SimpleNamespace(request=fail_send))
                host.workers = {"session": worker}
                host._stop_idle = AsyncMock()
                request = asyncio.create_task(host.request("view", "session", {}))
                host._track("session", request)
                await sent.wait()
                await asyncio.sleep(0)
                try:
                    self.assertFalse(request.done())
                finally:
                    worker.failure = WorkerFailed("session", ProcessFailure(
                        "RuntimeError", "child initialization failed", "original traceback",
                    ))
                    worker.exited.set()
                    with self.assertRaises(WorkerFailed) as caught:
                        await request
                self.assertIs(caught.exception, worker.failure)
                await asyncio.sleep(0)
                self.assertTrue(host.failures.empty())
                self.assertEqual(worker.requests, 0)
                host._stop_idle.assert_not_awaited()

    async def test_startup_policy_does_not_send_to_a_worker_already_failed(self):
        host = object.__new__(HostSupervisor)
        host.closed = False
        host.locks = {}
        host.workers = {}
        host._auto_authorize_lock = asyncio.Lock()
        peer = SimpleNamespace(request=AsyncMock(side_effect=AssertionError("closed pipe")))
        worker = Worker(None, peer)
        worker.failure = WorkerFailed("session", ProcessFailure(
            "RuntimeError", "child initialization failed", "original traceback",
        ))
        worker.exited.set()
        host._start = AsyncMock(return_value=worker)
        with self.assertRaises(WorkerFailed) as caught:
            await host.request("view", "session", {})
        self.assertIs(caught.exception, worker.failure)
        peer.request.assert_not_awaited()
