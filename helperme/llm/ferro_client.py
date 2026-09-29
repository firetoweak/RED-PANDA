"""OpenAI-compatible HTTP client for the separately managed Ferro Gateway."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from inspect import isawaitable
import json

import httpx

from helperme.llm.api import (
    ContentDeltaSink,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)
from helperme.llm.config import GatewayConfig
from helperme.llm.images import encode_images
from helperme.llm.types import (
    InvalidLLMResponse,
    LLMCallResult,
    LLMResponse,
    LLMUsage,
    ToolCall,
)
_NORMALIZED_DELTA_FIELDS = frozenset({"role", "content", "tool_calls"})
_CLIENT_MANAGED_OPTIONS = frozenset({
    "model",
    "messages",
    "tools",
    "tool_choice",
    "stream",
    "stream_options",
    "n",
})
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_AUTHENTICATION_CODES = frozenset({
    "authentication_required",
    "invalid_api_key",
    "missing_api_key",
    "insufficient_scope",
    "upstream_auth_error",
})


@dataclass
class _ToolCallParts:
    id: str = ""
    name: str = ""
    arguments: str = ""


class FerroClient:
    """A Host-owned LLMApi client; it does not start or supervise Ferro."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        request_options: dict[str, object] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._request_options = {} if request_options is None else request_options
        if type(self._request_options) is not dict:
            raise ValueError("request_options must be a dict")
        if any(type(key) is not str for key in self._request_options):
            raise ValueError("request_options keys must be strings")
        reserved = _CLIENT_MANAGED_OPTIONS & self._request_options.keys()
        if reserved:
            raise ValueError(
                "request_options cannot override client-managed fields: "
                + ", ".join(sorted(reserved))
            )
        try:
            json.dumps(self._request_options, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("request_options must contain JSON values") from exc
        self._read_attachment = None
        self._client = httpx.AsyncClient(
            base_url=f"{config.base_url}/",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Accept": "text/event-stream",
            },
            timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10),
            transport=transport,
        )

    def bind_attachment_reader(self, read: Callable[[str], bytes]) -> None:
        self._read_attachment = read

    async def __aenter__(self) -> "FerroClient":
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
        request_messages = encode_images(messages, self._read_attachment)
        payload: dict[str, object] = {
            **self._request_options,
            "model": model,
            "messages": request_messages,
            "tools": tools if tools is not None else [],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tool_choice"] = "auto"

        content_parts: list[str] = []
        extensions: dict[str, object] = {}
        tool_calls: dict[int, _ToolCallParts] = {}
        usage: LLMUsage | None = None
        received_done = False

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
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].lstrip()
                    if data == "[DONE]":
                        received_done = True
                        break

                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise InvalidLLMResponse(
                            "invalid_llm_response",
                            "Ferro stream event is not valid JSON",
                        ) from exc
                    if type(chunk) is not dict:
                        raise InvalidLLMResponse(
                            "invalid_llm_response",
                            "Ferro stream event must be an object",
                        )
                    if "error" in chunk:
                        self._raise_stream_error(chunk["error"])

                    if "usage" in chunk and chunk["usage"] is not None:
                        usage = self._parse_usage(chunk["usage"])

                    if "choices" not in chunk or type(chunk["choices"]) is not list:
                        raise InvalidLLMResponse(
                            "invalid_llm_response",
                            "Ferro stream event choices must be an array",
                        )
                    choices = chunk["choices"]
                    if not choices:
                        continue
                    if len(choices) != 1 or type(choices[0]) is not dict:
                        raise InvalidLLMResponse(
                            "invalid_llm_response",
                            "Ferro stream must contain exactly one choice",
                        )

                    choice = choices[0]
                    delta = choice.get("delta")
                    if type(delta) is not dict:
                        raise InvalidLLMResponse(
                            "invalid_llm_response",
                            "Ferro stream choice delta must be an object",
                        )

                    content = delta.get("content")
                    if content is not None:
                        if type(content) is not str:
                            raise InvalidLLMResponse(
                                "invalid_llm_response",
                                "Ferro content delta must be str|null",
                            )
                        if content:
                            content_parts.append(content)
                            await self._emit(on_content_delta, content)

                    raw_tool_calls = delta.get("tool_calls")
                    if raw_tool_calls is not None:
                        self._collect_tool_calls(raw_tool_calls, tool_calls)

                    for key, value in delta.items():
                        if key in _NORMALIZED_DELTA_FIELDS or value is None:
                            continue
                        self._collect_extension(extensions, key, value)
                        if key == "reasoning_content" and value:
                            await self._emit(on_reasoning_delta, value)

        except httpx.TransportError as exc:
            raise LLMTransientError(f"Ferro transport failed: {exc}") from exc

        if not received_done:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "Ferro stream ended without the [DONE] marker",
            )
        if usage is None:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "Ferro stream is missing terminal usage",
            )

        calls = tuple(
            ToolCall(parts.id, parts.name, parts.arguments)
            for _, parts in sorted(tool_calls.items())
        )
        return LLMCallResult(
            response=LLMResponse(
                content="".join(content_parts),
                calls=calls,
                message_extensions=extensions,
            ),
            usage=usage,
        )

    async def list_models(self) -> tuple[str, ...]:
        """Return model IDs that Ferro exposes to this caller."""
        try:
            response = await self._client.get("models")
        except httpx.TransportError as exc:
            raise LLMTransientError(f"Ferro transport failed: {exc}") from exc
        if not 200 <= response.status_code < 300:
            self._raise_http_error(response.status_code, response.text)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "Ferro model list is not valid JSON",
            ) from exc
        if type(payload) is not dict or type(payload.get("data")) is not list:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "Ferro model list must contain a data array",
            )
        models: list[str] = []
        for index, item in enumerate(payload["data"]):
            if type(item) is not dict or type(item.get("id")) is not str or not item["id"]:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    f"Ferro model list item[{index}] must have a non-empty string id",
                )
            models.append(item["id"])
        return tuple(models)

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
                "Ferro tool_calls delta must be an array|null",
            )
        for raw_call in raw_calls:
            if type(raw_call) is not dict:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "Ferro tool call delta must be an object",
                )
            index = raw_call.get("index")
            if type(index) is not int or index < 0:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "Ferro tool call delta index must be a nonnegative integer",
                )
            parts = collected.setdefault(index, _ToolCallParts())
            call_id = raw_call.get("id", "")
            if type(call_id) is not str:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "Ferro tool call id delta must be a string",
                )
            parts.id += call_id

            function = raw_call.get("function", {})
            if type(function) is not dict:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "Ferro tool call function delta must be an object",
                )
            name = function.get("name", "")
            arguments = function.get("arguments", "")
            if type(name) is not str or type(arguments) is not str:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "Ferro tool call name and arguments deltas must be strings",
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
                f"Ferro message extension {key!r} is not JSON data",
            ) from exc
        if type(value) is str:
            previous = extensions.get(key, "")
            if type(previous) is not str:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    f"Ferro message extension {key!r} changed type during the stream",
                )
            extensions[key] = previous + value
        elif key not in extensions:
            extensions[key] = value
        elif extensions[key] != value:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                f"Ferro message extension {key!r} changed during the stream",
            )

    @staticmethod
    def _parse_usage(raw_usage: object) -> LLMUsage:
        if type(raw_usage) is not dict:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "Ferro usage must be an object",
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
                "Ferro usage is missing prompt_tokens or completion_tokens",
            ) from exc
        except TypeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                f"Ferro usage fields are invalid: {exc}",
            ) from exc
        return LLMUsage(input_tokens, output_tokens, cached_tokens)

    @classmethod
    def _raise_http_error(cls, status_code: int, body: str) -> None:
        code, message = cls._error_fields(body)
        cls._raise_llm_error(status_code, code, message)

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

    @classmethod
    def _raise_stream_error(cls, raw_error: object) -> None:
        if type(raw_error) is not dict:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "Ferro stream error must be an object",
            )
        code = raw_error.get("code")
        message = raw_error.get("message")
        if type(code) is not str or type(message) is not str:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "Ferro stream error must include string code and message",
            )
        cls._raise_llm_error(None, code, message)

    @staticmethod
    def _raise_llm_error(
        status_code: int | None,
        code: str | None,
        message: str,
    ) -> None:
        detail = f"Ferro {status_code}" if status_code is not None else "Ferro stream"
        if code:
            detail += f" ({code})"
        detail += f": {message}"

        if status_code in {401, 403} or code in _AUTHENTICATION_CODES:
            raise LLMAuthenticationError(detail)
        if code == "context_length_exceeded":
            raise LLMContextLengthError(detail)
        if (
            code in {"stream_error", "stream_timeout"}
            or status_code in _TRANSIENT_STATUS_CODES
        ):
            raise LLMTransientError(detail)
        raise LLMProviderError(detail)
