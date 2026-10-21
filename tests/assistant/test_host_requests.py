from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from redpanda.assistant.host.ipc import ProcessFailure, WorkerFailed
from redpanda.assistant.host.supervisor import HostSupervisor, Worker


class HostRequestFailureTest(unittest.IsolatedAsyncioTestCase):
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
