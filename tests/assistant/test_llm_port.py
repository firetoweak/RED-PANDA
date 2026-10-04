from __future__ import annotations

import asyncio
import multiprocessing
import unittest

from redpanda.assistant.host.ipc import PipePeer
from redpanda.assistant.host.llm_port import WorkerLlmPort, complete_llm_chat
from redpanda.llm.api import LLMTransientError
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage


class _FakeLlm:
    async def chat(
        self,
        messages,
        model,
        tools=None,
        *,
        on_content_delta=None,
        on_reasoning_delta=None,
    ):
        if model == "fail":
            raise LLMTransientError("provider timeout")
        if on_reasoning_delta is not None:
            emitted = on_reasoning_delta("想")
            if asyncio.iscoroutine(emitted):
                await emitted
        if on_content_delta is not None:
            emitted = on_content_delta("hel")
            if asyncio.iscoroutine(emitted):
                await emitted
            emitted = on_content_delta("lo")
            if asyncio.iscoroutine(emitted):
                await emitted
        return LLMCallResult(
            LLMResponse(content="hello"),
            LLMUsage(1, 1),
        )


class LlmPortTest(unittest.IsolatedAsyncioTestCase):
    async def test_port_streams_deltas_and_reconstructs_errors(self):
        left, right = multiprocessing.Pipe()
        host_box: list[PipePeer] = []

        async def host_handle(operation, session_id, arguments):
            self.assertEqual(operation, "llm_chat")
            self.assertNotIn("session_id", arguments)

            async def on_delta(text):
                await host_box[0].send(("delta", host_box[0].active_request_id, text))

            async def on_reasoning_delta(text):
                await host_box[0].send(
                    ("reasoning_delta", host_box[0].active_request_id, text)
                )

            return await complete_llm_chat(
                _FakeLlm(), arguments, on_delta, on_reasoning_delta
            )

        async def unused_handle(operation, session_id, arguments):
            raise AssertionError(f"unexpected worker request {operation}")

        async def unused_signal(kind, *payload):
            raise AssertionError(f"unexpected signal {kind}")

        host = PipePeer(left, host_handle, unused_signal)
        worker = PipePeer(right, unused_handle, unused_signal)
        host_box.append(host)
        port = WorkerLlmPort(worker, "session-a")
        host_reader = asyncio.create_task(host.run())
        worker_reader = asyncio.create_task(worker.run())
        deltas: list[str] = []
        thoughts: list[str] = []
        try:
            result = await port.chat(
                [{"role": "user", "content": "hi"}],
                "assistant",
                on_content_delta=deltas.append,
                on_reasoning_delta=thoughts.append,
            )
            self.assertEqual(result.response.content, "hello")
            self.assertEqual(deltas, ["hel", "lo"])
            self.assertEqual(thoughts, ["想"])
            with self.assertRaises(LLMTransientError):
                await port.chat([{"role": "user", "content": "hi"}], "fail")
        finally:
            await host.close(RuntimeError("done"))
            await worker.close(RuntimeError("done"))
            host_reader.cancel()
            worker_reader.cancel()
            await asyncio.gather(host_reader, worker_reader, return_exceptions=True)
            left.close()
            right.close()
