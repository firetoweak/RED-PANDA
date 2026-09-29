from __future__ import annotations

import json
import unittest

import httpx

from helperme.llm.api import (
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMTransientError,
)
from helperme.llm.config import GatewayConfig
from helperme.llm.ferro_client import FerroClient
from helperme.llm.types import InvalidLLMResponse


def _config() -> GatewayConfig:
    return GatewayConfig(
        base_url="http://127.0.0.1:8080/v1",
        api_key="ferro-key",
    )


def _stream(*chunks: dict[str, object]) -> bytes:
    return b"".join(
        b"data: " + json.dumps(chunk).encode() + b"\n\n"
        for chunk in chunks
    ) + b"data: [DONE]\n\n"


class FerroClientStreamingTest(unittest.IsolatedAsyncioTestCase):
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

        client = FerroClient(
            _config(),
            request_options={"reasoning_effort": "high"},
            transport=httpx.MockTransport(handle),
        )
        content: list[str] = []
        reasoning: list[str] = []
        async with client:
            result = await client.chat(
                [{"role": "user", "content": "read"}],
                "logical-model",
                [{"type": "function", "function": {"name": "read_file"}}],
                on_content_delta=content.append,
                on_reasoning_delta=reasoning.append,
            )

        request = requests[0]
        payload = json.loads(request.content)
        self.assertEqual(str(request.url), "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer ferro-key")
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

    async def test_encodes_images_at_the_client_boundary(self):
        body = _stream(
            {"choices": [{"delta": {"content": "done"}}]},
            {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
        )
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=body)

        client = FerroClient(
            _config(),
            transport=httpx.MockTransport(handle),
        )
        client.bind_attachment_reader(lambda attachment_id: b"image bytes")
        async with client:
            await client.chat(
                [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image", "id": "image-1", "mime": "image/png"},
                    ],
                }],
                "logical-model",
            )

        content = json.loads(requests[0].content)["messages"][0]["content"]
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,aW1hZ2UgYnl0ZXM=")

    async def test_http_error_codes_map_to_helperme_errors(self):
        cases = (
            (401, "invalid_api_key", LLMAuthenticationError),
            (400, "context_length_exceeded", LLMContextLengthError),
            (429, "rate_limit_exceeded", LLMTransientError),
        )
        for status, code, error_type in cases:
            with self.subTest(status=status, code=code):
                client = FerroClient(
                    _config(),
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            status,
                            json={"error": {"code": code, "message": "upstream"}},
                        )
                    ),
                )
                async with client:
                    with self.assertRaisesRegex(error_type, "upstream"):
                        await client.chat([], "logical-model")

    async def test_mid_stream_error_is_reported_as_transient(self):
        body = (
            b'data: {"error":{"code":"stream_timeout",'
            b'"message":"gateway timed out"}}\n\n'
        )
        client = FerroClient(
            _config(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=body)
            ),
        )
        async with client:
            with self.assertRaisesRegex(LLMTransientError, "gateway timed out"):
                await client.chat([], "logical-model")

    async def test_requires_done_marker_and_usage(self):
        cases = (
            (b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":0}}\n\n', "DONE"),
            (_stream({"choices": [{"delta": {"content": "done"}}]}), "usage"),
        )
        for body, message in cases:
            with self.subTest(message=message):
                client = FerroClient(
                    _config(),
                    transport=httpx.MockTransport(
                        lambda request, body=body: httpx.Response(200, content=body)
                    ),
                )
                async with client:
                    with self.assertRaisesRegex(InvalidLLMResponse, message):
                        await client.chat([], "logical-model")

    async def test_callback_failure_is_not_wrapped(self):
        client = FerroClient(
            _config(),
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
                await client.chat([], "logical-model", on_content_delta=fail)
