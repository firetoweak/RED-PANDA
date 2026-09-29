"""OpenAI-compatible Chat Completions client for one built-in provider."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from inspect import isawaitable
import json

import httpx

from thinllm.errors import (
    InvalidLLMResponse,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)
from thinllm.providers import Endpoint
from thinllm.types import (
    ContentDeltaSink,
    LLMCallResult,
    LLMResponse,
    LLMUsage,
    ToolCall,
)
_NORMALIZED_DELTA_FIELDS = frozenset({"role", "content", "tool_calls"})
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_RETRY_DELAYS = (0.5, 1.0, 2.0)


@dataclass
class _ToolCallParts:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class _Stream:
    started: bool = False
    received_done: bool = False
    content_parts: list[str] = field(default_factory=list)
    extensions: dict[str, object] = field(default_factory=dict)
    tool_calls: dict[int, _ToolCallParts] = field(default_factory=dict)
    usage: LLMUsage | None = None


class ChatCompletionsClient:
    """A streaming chat client bound to one provider endpoint."""

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        request_options: dict[str, object] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._request_options = {} if request_options is None else request_options
        headers = {"Accept": "text/event-stream"}
        if endpoint.api_key is not None:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        self._client = httpx.AsyncClient(
            base_url=f"{endpoint.base_url}/",
            headers=headers,
            timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10),
            transport=transport,
        )

    async def __aenter__(self) -> "ChatCompletionsClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, object]],
        model: str,
        tools: list[dict[str, object]] | None = None,
        *,
        on_content_delta: ContentDeltaSink | None = None,
        on_reasoning_delta: ContentDeltaSink | None = None,
    ) -> LLMCallResult:
        if self._endpoint.pads_reasoning_content:
            messages = [
                (
                    {**message, "reasoning_content": ""}
                    if message.get("role") == "assistant"
                    and "reasoning_content" not in message
                    else message
                )
                for message in messages
            ]
        payload: dict[str, object] = {
            **self._request_options,
            "model": model.partition("/")[2],
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        attempt = 0
        while True:
            stream = _Stream()
            try:
                await self._stream(payload, stream, on_content_delta, on_reasoning_delta)
            except LLMTransientError:
                if stream.started or attempt == len(_RETRY_DELAYS):
                    raise
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                attempt += 1
                continue
            return self._result(stream)

    async def _stream(
        self,
        payload: dict[str, object],
        stream: _Stream,
        on_content_delta: ContentDeltaSink | None,
        on_reasoning_delta: ContentDeltaSink | None,
    ) -> None:
        try:
            async with self._client.stream(
                "POST",
                "chat/completions",
                json=payload,
            ) as response:
                if not 200 <= response.status_code < 300:
                    await response.aread()
                    self._raise_http_error(response.status_code, response.text)

                async for line in response.aiter_lines():
                    stream.started = True
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].lstrip()
                    if data == "[DONE]":
                        stream.received_done = True
                        break
                    await self._consume(
                        data, stream, on_content_delta, on_reasoning_delta
                    )
        except httpx.TransportError as exc:
            raise LLMTransientError(
                f"{self._endpoint.provider} transport failed: {exc}"
            ) from exc

    async def _consume(
        self,
        data: str,
        stream: _Stream,
        on_content_delta: ContentDeltaSink | None,
        on_reasoning_delta: ContentDeltaSink | None,
    ) -> None:
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream event is not valid JSON",
            ) from exc
        if type(chunk) is not dict:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream event must be an object",
            )
        if "error" in chunk:
            self._raise_stream_error(chunk["error"])

        if "usage" in chunk and chunk["usage"] is not None:
            stream.usage = self._parse_usage(chunk["usage"])

        if "choices" not in chunk or type(chunk["choices"]) is not list:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream event choices must be an array",
            )
        choices = chunk["choices"]
        if not choices:
            return
        if len(choices) != 1 or type(choices[0]) is not dict:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream must contain exactly one choice",
            )

        delta = choices[0].get("delta")
        if type(delta) is not dict:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream choice delta must be an object",
            )

        content = delta.get("content")
        if content is not None:
            if type(content) is not str:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "model content delta must be str|null",
                )
            if content:
                stream.content_parts.append(content)
                await self._emit(on_content_delta, content)

        raw_tool_calls = delta.get("tool_calls")
        if raw_tool_calls is not None:
            self._collect_tool_calls(raw_tool_calls, stream.tool_calls)

        for key, value in delta.items():
            if key in _NORMALIZED_DELTA_FIELDS or value is None:
                continue
            self._collect_extension(stream.extensions, key, value)
            if key == "reasoning_content" and value:
                await self._emit(on_reasoning_delta, value)

    @staticmethod
    def _result(stream: _Stream) -> LLMCallResult:
        if not stream.received_done:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream ended without the [DONE] marker",
            )
        if stream.usage is None:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream is missing terminal usage",
            )
        calls = tuple(
            ToolCall(parts.id, parts.name, parts.arguments)
            for _, parts in sorted(stream.tool_calls.items())
        )
        return LLMCallResult(
            response=LLMResponse(
                content="".join(stream.content_parts),
                calls=calls,
                message_extensions=stream.extensions,
            ),
            usage=stream.usage,
        )

    @staticmethod
    async def _emit(sink: ContentDeltaSink | None, value: str) -> None:
        if sink is None:
            return
        emitted = sink(value)
        if isawaitable(emitted):
            await emitted

    @staticmethod
    def _collect_tool_calls(
        raw_calls: object,
        collected: dict[int, _ToolCallParts],
    ) -> None:
        if type(raw_calls) is not list:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model tool_calls delta must be an array|null",
            )
        for raw_call in raw_calls:
            if type(raw_call) is not dict:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "model tool call delta must be an object",
                )
            index = raw_call.get("index")
            if type(index) is not int or index < 0:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "model tool call delta index must be a nonnegative integer",
                )
            parts = collected.setdefault(index, _ToolCallParts())
            call_id = raw_call.get("id", "")
            if type(call_id) is not str:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "model tool call id delta must be a string",
                )
            parts.id += call_id

            function = raw_call.get("function", {})
            if type(function) is not dict:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "model tool call function delta must be an object",
                )
            name = function.get("name", "")
            arguments = function.get("arguments", "")
            if type(name) is not str or type(arguments) is not str:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "model tool call name and arguments deltas must be strings",
                )
            parts.name += name
            parts.arguments += arguments

    @staticmethod
    def _collect_extension(
        extensions: dict[str, object],
        key: str,
        value: object,
    ) -> None:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                f"model message extension {key!r} is not JSON data",
            ) from exc
        if type(value) is str:
            previous = extensions.get(key, "")
            if type(previous) is not str:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    f"model message extension {key!r} changed type during the stream",
                )
            extensions[key] = previous + value
        elif key not in extensions:
            extensions[key] = value
        elif extensions[key] != value:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                f"model message extension {key!r} changed during the stream",
            )

    @staticmethod
    def _parse_usage(raw_usage: object) -> LLMUsage:
        if type(raw_usage) is not dict:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model usage must be an object",
            )
        try:
            input_tokens = raw_usage["prompt_tokens"]
            output_tokens = raw_usage["completion_tokens"]
            details = raw_usage.get("prompt_tokens_details")
            if details is None:
                cached_tokens = 0
            elif type(details) is dict:
                cached_tokens = details.get("cached_tokens", 0)
                if cached_tokens is None:
                    cached_tokens = 0
            else:
                raise TypeError("prompt_tokens_details must be an object|null")
        except KeyError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model usage is missing prompt_tokens or completion_tokens",
            ) from exc
        except TypeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                f"model usage fields are invalid: {exc}",
            ) from exc
        return LLMUsage(input_tokens, output_tokens, cached_tokens)

    def _raise_http_error(self, status_code: int, body: str) -> None:
        code, message = self._error_fields(body)
        self._raise_llm_error(status_code, code, message)

    @staticmethod
    def _error_fields(body: str) -> tuple[str | None, str]:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None, body
        if type(payload) is dict and type(payload.get("error")) is dict:
            error = payload["error"]
            code = error.get("code")
            message = error.get("message")
            return (
                code if type(code) is str else None,
                message if type(message) is str else body,
            )
        return None, body

    def _raise_stream_error(self, raw_error: object) -> None:
        if type(raw_error) is not dict or type(raw_error.get("message")) is not str:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream error must be an object with a string message",
            )
        code = raw_error.get("code")
        self._raise_llm_error(
            None, code if type(code) is str else None, raw_error["message"]
        )

    def _raise_llm_error(
        self,
        status_code: int | None,
        code: str | None,
        message: str,
    ) -> None:
        detail = self._endpoint.provider
        detail += f" {status_code}" if status_code is not None else " stream"
        if code:
            detail += f" ({code})"
        detail += f": {message}"

        if status_code in {401, 403}:
            raise LLMAuthenticationError(detail)
        if code == "context_length_exceeded":
            raise LLMContextLengthError(detail)
        if status_code in _TRANSIENT_STATUS_CODES:
            raise LLMTransientError(detail)
        raise LLMProviderError(detail)
