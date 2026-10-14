from __future__ import annotations

import asyncio
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

import httpx

from thinllm import (
    ChatCompletionsClient,
    Endpoint,
    InvalidLLMResponse,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMTransientError,
    resolve_endpoint,
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
            resolve_endpoint(
                "ollama/qwen3:8b", {"base_url": "http://127.0.0.1:11434/v1", "api_key": ""}
            ),
            transport=httpx.MockTransport(handle),
        )
        async with client:
            await client.chat([], "ollama/qwen3:8b")

        self.assertNotIn("authorization", requests[0].headers)
        self.assertEqual(
            str(requests[0].url), "http://127.0.0.1:11434/v1/chat/completions"
        )
        self.assertEqual(json.loads(requests[0].content)["model"], "qwen3:8b")

    async def test_reasoning_effort_is_sent_per_call_and_does_not_leak_to_the_next_call(self):
        payloads = []

        def handle(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, content=_DONE_OK)

        async with ChatCompletionsClient(
            resolve_endpoint("stepfun/step-5-preview", {"api_key": "test"}),
            transport=httpx.MockTransport(handle),
        ) as client:
            for effort in ("low", "medium", "high", None):
                await client.chat([], "stepfun/step-5-preview", reasoning_effort=effort)
        self.assertEqual([p.get("reasoning_effort") for p in payloads], ["low", "medium", "high", None])
        self.assertNotIn("reasoning_effort", payloads[-1])
        self.assertTrue(all(p["model"] == "step-5-preview" for p in payloads))

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
        self.assertNotIn("reasoning_content", history[1])

    async def test_preserved_thinking_options_follow_each_provider_and_model(self):
        cases = {
            "bigmodel": [
                ("glm-4.7", {"thinking": {"clear_thinking": False}}),
                ("glm-4.6", {}),
                ("glm-5.2", {"thinking": {"clear_thinking": False}}),
                ("glm-5.3-flash", {"thinking": {"clear_thinking": False}}),
                ("glm-unknown", {}),
            ],
            "qwen": [
                ("qwen3.6-plus", {"preserve_thinking": True}),
                ("qwen3-32b", {}),
                ("qwen3.7-max-2026-05-20", {"preserve_thinking": True}),
                ("qwen3.8-max", {"preserve_thinking": True}),
                ("qwen3.8-omni-flash", {"preserve_thinking": True}),
                ("qwen-unknown", {}),
            ],
            "openai": [("gpt-4.1", {})],
            "stepfun": [("step-5-preview", {})],
            "deepseek": [("deepseek-v4-pro", {})],
            "vllm": [("glm-5.2", {})],
            "ollama": [("qwen3.6-plus", {})],
        }
        history = [{"role": "assistant", "content": "answer", "reasoning_content": "  original\nreasoning  "}]
        for provider, models in cases.items():
            with self.subTest(provider=provider):
                payloads = []

                def handle(request):
                    payloads.append(json.loads(request.content))
                    return httpx.Response(200, content=_DONE_OK)

                connection = {"api_key": "test", "base_url": "http://localhost/v1"}
                async with ChatCompletionsClient(
                    resolve_endpoint(f"{provider}/{models[0][0]}", connection),
                    transport=httpx.MockTransport(handle),
                ) as client:
                    for model, expected in models:
                        await client.chat(history, f"{provider}/{model}")
                        payload = payloads[-1]
                        options = {key: payload[key] for key in ("thinking", "preserve_thinking") if key in payload}
                        self.assertEqual(options, expected, model)
                        self.assertEqual(payload["messages"], history)

    async def test_explicit_thinking_options_override_provider_defaults(self):
        for model, options in (
            ("bigmodel/glm-5.2", {"thinking": {"type": "disabled"}}),
            ("qwen/qwen3.6-plus", {"preserve_thinking": False}),
        ):
            with self.subTest(model=model):
                payloads = []

                def handle(request):
                    payloads.append(json.loads(request.content))
                    return httpx.Response(200, content=_DONE_OK)

                original = deepcopy(options)
                async with ChatCompletionsClient(
                    resolve_endpoint(model, {"api_key": "test"}),
                    request_options=options,
                    transport=httpx.MockTransport(handle),
                ) as client:
                    await client.chat([], model)
                for key, value in options.items():
                    self.assertEqual(payloads[0][key], value)
                self.assertEqual(options, original)

    async def test_all_native_reasoning_is_replayed_across_tools_and_user_turns(self):
        payloads = []
        reasoning_deltas = []
        originals = []
        history = [{"role": "user", "content": "inspect"}]
        tools = [{"type": "function", "function": {"name": "read_file"}}]

        def handle(request):
            payloads.append(json.loads(request.content))
            number = len(payloads)
            delta = {"content": f"answer {number}"}
            if number == 1:
                delta["tool_calls"] = [{"index": 0, "id": "call-1", "type": "function",
                                        "function": {"name": "read_file", "arguments": "{}"}}]
            return httpx.Response(200, content=_stream(
                {"choices": [{"delta": {"reasoning_content": f"  think {number}\n"}}]},
                {"choices": [{"delta": {"reasoning_content": "keep whitespace  "}}]},
                {"choices": [{"delta": {"native_state": {"signature": f"opaque-{number}"}}}]},
                {"choices": [{"delta": delta}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            ))

        async with ChatCompletionsClient(
            resolve_endpoint("deepseek/deepseek-v4-pro", {"api_key": "test"}),
            transport=httpx.MockTransport(handle),
        ) as client:
            for number in range(1, 4):
                before = deepcopy(history)
                result = await client.chat(history, "deepseek/deepseek-v4-pro", tools,
                                           on_reasoning_delta=reasoning_deltas.append)
                self.assertEqual(history, before)
                self.assertEqual(payloads[-1]["messages"], before)
                replayed = [message for message in payloads[-1]["messages"] if message["role"] == "assistant"]
                self.assertEqual(replayed, originals)
                response = result.response
                self.assertEqual(response.message_extensions, {
                    "reasoning_content": f"  think {number}\nkeep whitespace  ",
                    "native_state": {"signature": f"opaque-{number}"},
                })
                message = {"role": "assistant", "content": response.content, **response.message_extensions}
                if response.calls:
                    message["tool_calls"] = [{"id": response.calls[0].id, "type": "function",
                                              "function": {"name": "read_file", "arguments": "{}"}}]
                originals.append(deepcopy(message))
                history.append(message)
                history.append({"role": "tool", "tool_call_id": "call-1", "content": "file content"}
                               if number == 1 else {"role": "user", "content": "continue"})
        self.assertEqual(reasoning_deltas, [part for number in range(1, 4)
                                           for part in (f"  think {number}\n", "keep whitespace  ")])

    async def test_invalid_reasoning_delta_is_rejected_before_preview(self):
        for value in (1, True, [], {}):
            with self.subTest(value=value):
                reasoning = []
                async with ChatCompletionsClient(
                    _endpoint(),
                    transport=httpx.MockTransport(lambda request: httpx.Response(
                        200, content=_stream({"choices": [{"delta": {"reasoning_content": value}}]}),
                    )),
                ) as client:
                    with self.assertRaisesRegex(InvalidLLMResponse, "reasoning_content delta must be str"):
                        await client.chat([], "test/model", on_reasoning_delta=reasoning.append)
                self.assertEqual(reasoning, [])

    async def test_http_error_codes_map_to_redpanda_errors(self):
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

    async def test_attempt_timing_excludes_retry_wait_and_finishes_after_stream_end(self):
        clock = [0.0]
        first_delta = asyncio.Event()
        finish_stream = asyncio.Event()
        requests = []

        async def stream():
            clock[0] += 3
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            await finish_stream.wait()
            clock[0] += 4
            yield b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n'

        def handle(request):
            requests.append(request)
            if len(requests) == 1:
                clock[0] += 2
                return httpx.Response(503, json={"error": {"message": "busy"}})
            return httpx.Response(200, content=stream())

        async def retry_wait(_delay):
            clock[0] += 5

        with (
            patch("thinllm.chat_completions.perf_counter", lambda: clock[0]),
            patch("thinllm.chat_completions.asyncio.sleep", retry_wait),
            self.assertLogs("thinllm.chat_completions", level="INFO") as captured,
        ):
            async with ChatCompletionsClient(_endpoint(), transport=httpx.MockTransport(handle)) as client:
                task = asyncio.create_task(client.chat(
                    [{"role": "user", "content": "private request text"}], "test/logical-model",
                    on_content_delta=lambda _: first_delta.set(),
                ))
                await asyncio.wait_for(first_delta.wait(), 5)
                self.assertEqual([record.stage for record in captured.records], ["model_request", "model_retry_wait"])
                finish_stream.set()
                self.assertEqual((await task).response.content, "ok")
        self.assertEqual([record.elapsed_ms for record in captured.records], [2000, 5000, 7000])
        self.assertEqual([record.attempt for record in captured.records], [1, 1, 2])
        self.assertEqual([record.outcome for record in captured.records], ["failed", "completed", "completed"])
        for record in captured.records:
            self.assertNotIn("private request text", str(record.__dict__))
            self.assertNotIn("provider-key", str(record.__dict__))

    async def test_retry_exhaustion_records_four_attempts_and_three_separate_waits(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(503, json={"error": {"message": "busy"}})

        with self.assertLogs("thinllm.chat_completions", level="INFO") as captured:
            async with ChatCompletionsClient(_endpoint(), transport=httpx.MockTransport(handle)) as client:
                with self.assertRaises(LLMTransientError):
                    await client.chat([], "test/logical-model")
        self.assertEqual(len(requests), 4)
        attempts = [record for record in captured.records if record.stage == "model_request"]
        waits = [record for record in captured.records if record.stage == "model_retry_wait"]
        self.assertEqual([record.attempt for record in attempts], [1, 2, 3, 4])
        self.assertEqual([record.attempt for record in waits], [1, 2, 3])
        self.assertTrue(all(record.outcome == "failed" for record in attempts))

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
