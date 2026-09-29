from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx

from thinllm import (
    ChatCompletionsClient,
    Endpoint,
    InvalidLLMResponse,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMTransientError,
)


def _endpoint(
    *, api_key: str | None = "provider-key", pads_reasoning_content: bool = False
) -> Endpoint:
    return Endpoint(
        provider="test",
        base_url="http://127.0.0.1:8080/v1",
        api_key=api_key,
        pads_reasoning_content=pads_reasoning_content,
    )


def _stream(*chunks: dict[str, object]) -> bytes:
    return b"".join(
        b"data: " + json.dumps(chunk).encode() + b"\n\n"
        for chunk in chunks
    ) + b"data: [DONE]\n\n"


_DONE_OK = _stream(
    {"choices": [{"delta": {"content": "ok"}}]},
    {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
)


class ChatCompletionsStreamingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        retry_delays = patch("thinllm.chat_completions._RETRY_DELAYS", (0, 0, 0))
        retry_delays.start()
        self.addCleanup(retry_delays.stop)

    async def test_streams_content_reasoning_tools_and_usage(self):
        body = _stream(
            {"choices": [{"delta": {"role": "assistant", "content": "hel"}}]},
            {
                "choices": [{"delta": {
                    "content": "lo",
                    "reasoning_content": "considering",
                }}],
            },
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "id": "call-",
                "type": "function",
                "function": {"name": "read_", "arguments": "{"},
            }]}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "id": "1",
                "function": {"name": "file", "arguments": "}"},
            }]}}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 96},
                },
            },
        )
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        client = ChatCompletionsClient(
            _endpoint(),
            request_options={"reasoning_effort": "high"},
            transport=httpx.MockTransport(handle),
        )
        content: list[str] = []
        reasoning: list[str] = []
        async with client:
            result = await client.chat(
                [{"role": "user", "content": "read"}],
                "test/logical-model",
                [{"type": "function", "function": {"name": "read_file"}}],
                on_content_delta=content.append,
                on_reasoning_delta=reasoning.append,
            )

        request = requests[0]
        payload = json.loads(request.content)
        self.assertEqual(str(request.url), "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer provider-key")
        self.assertEqual(payload["model"], "logical-model")
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(content, ["hel", "lo"])
        self.assertEqual(reasoning, ["considering"])
        self.assertEqual(result.response.content, "hello")
        self.assertEqual(result.response.message_extensions["reasoning_content"], "considering")
        self.assertEqual(result.response.calls[0].id, "call-1")
        self.assertEqual(result.response.calls[0].name, "read_file")
        self.assertEqual(result.response.calls[0].arguments, "{}")
        self.assertEqual(result.usage.input_tokens, 120)
        self.assertEqual(result.usage.output_tokens, 5)
        self.assertEqual(result.usage.cached_input_tokens, 96)

    async def test_keyless_provider_sends_no_authorization(self):
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=_DONE_OK)

        client = ChatCompletionsClient(
            _endpoint(api_key=None), transport=httpx.MockTransport(handle)
        )
        async with client:
            await client.chat([], "test/logical-model")

        self.assertNotIn("authorization", requests[0].headers)

    async def test_only_padding_providers_fill_missing_reasoning_content(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "plain"},
            {"role": "assistant", "content": "kept", "reasoning_content": "why"},
        ]
        for pads in (True, False):
            with self.subTest(pads_reasoning_content=pads):
                requests: list[httpx.Request] = []

                def handle(request: httpx.Request) -> httpx.Response:
                    requests.append(request)
                    return httpx.Response(200, content=_DONE_OK)

                client = ChatCompletionsClient(
                    _endpoint(pads_reasoning_content=pads),
                    transport=httpx.MockTransport(handle),
                )
                async with client:
                    await client.chat(history, "test/logical-model")

                messages = json.loads(requests[0].content)["messages"]
                self.assertNotIn("reasoning_content", messages[0])
                if pads:
                    self.assertEqual(messages[1]["reasoning_content"], "")
                else:
                    self.assertNotIn("reasoning_content", messages[1])
                self.assertEqual(messages[2]["reasoning_content"], "why")

    async def test_http_error_codes_map_to_helperme_errors(self):
        cases = (
            (401, "invalid_api_key", LLMAuthenticationError),
            (400, "context_length_exceeded", LLMContextLengthError),
            (429, "rate_limit_exceeded", LLMTransientError),
        )
        for status, code, error_type in cases:
            with self.subTest(status=status, code=code):
                client = ChatCompletionsClient(
                    _endpoint(),
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            status,
                            json={"error": {"code": code, "message": "upstream"}},
                        )
                    ),
                )
                async with client:
                    with self.assertRaisesRegex(error_type, "upstream"):
                        await client.chat([], "test/logical-model")

    async def test_transient_failure_before_the_stream_is_retried(self):
        responses = [httpx.Response(503, json={"error": {"message": "busy"}})]

        def handle(request: httpx.Request) -> httpx.Response:
            return responses.pop(0) if responses else httpx.Response(200, content=_DONE_OK)

        client = ChatCompletionsClient(_endpoint(), transport=httpx.MockTransport(handle))
        async with client:
            result = await client.chat([], "test/logical-model")

        self.assertEqual(result.response.content, "ok")

    async def test_failure_after_the_stream_started_is_not_retried(self):
        requests: list[httpx.Request] = []

        async def interrupted():
            yield b'data: {"choices":[{"delta":{"content":"par"}}]}\n\n'
            raise httpx.ReadError("connection reset")

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=interrupted())

        client = ChatCompletionsClient(_endpoint(), transport=httpx.MockTransport(handle))
        async with client:
            with self.assertRaisesRegex(LLMTransientError, "connection reset"):
                await client.chat([], "test/logical-model")

        self.assertEqual(len(requests), 1)

    async def test_requires_done_marker_and_usage(self):
        cases = (
            (b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":0}}\n\n', "DONE"),
            (_stream({"choices": [{"delta": {"content": "done"}}]}), "usage"),
        )
        for body, message in cases:
            with self.subTest(message=message):
                client = ChatCompletionsClient(
                    _endpoint(),
                    transport=httpx.MockTransport(
                        lambda request, body=body: httpx.Response(200, content=body)
                    ),
                )
                async with client:
                    with self.assertRaisesRegex(InvalidLLMResponse, message):
                        await client.chat([], "test/logical-model")

    async def test_callback_failure_is_not_wrapped(self):
        client = ChatCompletionsClient(
            _endpoint(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=_stream(
                    {"choices": [{"delta": {"content": "shown"}}]},
                    {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                ))
            ),
        )

        def fail(_content: str) -> None:
            raise RuntimeError("preview failed")

        async with client:
            with self.assertRaisesRegex(RuntimeError, "preview failed"):
                await client.chat([], "test/logical-model", on_content_delta=fail)
