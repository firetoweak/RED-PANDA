"""LiteLLM Router 到 HelperMe 窄协议的进程内适配。"""

from __future__ import annotations

import os
from copy import deepcopy
from hashlib import sha256
from importlib import import_module
from importlib.util import find_spec
from inspect import isawaitable
from pathlib import Path
from typing import Any

from helperme.llm.api import (
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)
from helperme.llm.config import ModelConfig
from helperme.llm.images import encode_images
from helperme.llm.types import (
    InvalidLLMResponse,
    LLMCallResult,
    LLMResponse,
    LLMUsage,
    ToolCall,
)


_NORMALIZED_MESSAGE_FIELDS = frozenset({"role", "content", "tool_calls"})
_BUNDLED_TIKTOKEN_RESOURCES = {
    "9b5ad71b2ce5302211f9c61530b329a4922fc6a4": (
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    ),
    "ec7223a39ce59f226a68acc30dc1af2788490e15": (
        "94b5ca7dff4d00767bc256fdd1b27e5b17361d7b8a5f968547f9f23eb70d2069"
    ),
    "fb374d419588a4632f3f557e76b4b70aebbca790": (
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
    ),
}
_CONTEXT_LIMIT_ERROR_MARKERS = (
    "context length",
    "maximum context",
    "max context",
    "context window",
    "token limit",
    "tokens exceed",
    "too many tokens",
    "input is too long",
)


def _is_context_limit_error(error: str) -> bool:
    text = error.lower()
    return any(marker in text for marker in _CONTEXT_LIMIT_ERROR_MARKERS)


def _initialize_bundled_tokenizers() -> None:
    """修正 wheel 在 Windows 上的换行转换，并拒绝缺损的离线资源。"""
    spec = find_spec("litellm")
    if spec is None or spec.submodule_search_locations is None:
        raise ModuleNotFoundError("litellm is not installed")
    package_root = Path(next(iter(spec.submodule_search_locations)))
    tokenizer_root = package_root / "litellm_core_utils" / "tokenizers"
    for filename, expected_hash in _BUNDLED_TIKTOKEN_RESOURCES.items():
        path = tokenizer_root / filename
        content = path.read_bytes()
        if sha256(content).hexdigest() == expected_hash:
            continue
        normalized = content.replace(b"\r\n", b"\n")
        if sha256(normalized).hexdigest() != expected_hash:
            raise RuntimeError(f"LiteLLM bundled tokenizer is invalid: {path}")
        path.write_bytes(normalized)


class LiteLLMAdapter:
    def __init__(self, config: ModelConfig):
        # LiteLLM 在 import 时读取模型资料表；固定使用随包安装的本地副本。
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        # LiteLLM 默认使用随包安装的 tokenizer。外部遗留值会覆盖这个默认值，
        # 并在缓存缺失时触发 tiktoken 下载，因此这里明确移除。
        os.environ.pop("CUSTOM_TIKTOKEN_CACHE_DIR", None)
        _initialize_bundled_tokenizers()
        self._litellm = import_module("litellm")
        self._router = self._litellm.Router(**deepcopy(config.router))
        self._read_attachment = None

    def bind_attachment_reader(self, read) -> None:
        self._read_attachment = read

    async def __aenter__(self) -> "LiteLLMAdapter":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self._litellm.close_litellm_async_clients()

    async def chat(
        self,
        messages,
        model,
        tools=None,
        *,
        on_content_delta=None,
        on_reasoning_delta=None,
    ) -> LLMCallResult:
        request_messages = encode_images(messages, self._read_attachment)
        content_parts: list[str] = []
        try:
            stream = await self._completion(model, request_messages, tools)
            chunks = []
            async for chunk in stream:
                chunks.append(chunk)
                content = self._content_delta(chunk)
                if content:
                    content_parts.append(content)
                    if on_content_delta is not None:
                        emitted = on_content_delta(content)
                        if isawaitable(emitted):
                            await emitted
                reasoning = self._reasoning_delta(chunk)
                if reasoning and on_reasoning_delta is not None:
                    emitted = on_reasoning_delta(reasoning)
                    if isawaitable(emitted):
                        await emitted
            completion = self._litellm.stream_chunk_builder(
                chunks,
                messages=request_messages,
            )
        except self._litellm.ContextWindowExceededError as exc:
            raise LLMContextLengthError(str(exc)) from exc
        except (
            self._litellm.AuthenticationError,
            self._litellm.PermissionDeniedError,
        ) as exc:
            raise LLMAuthenticationError(str(exc)) from exc
        except (
            self._litellm.APIConnectionError,
            self._litellm.Timeout,
            self._litellm.RateLimitError,
            self._litellm.InternalServerError,
            self._litellm.ServiceUnavailableError,
        ) as exc:
            raise LLMTransientError(str(exc)) from exc
        except self._litellm.APIError as exc:
            error = str(exc)
            if _is_context_limit_error(error):
                raise LLMContextLengthError(error) from exc
            raise LLMProviderError(error) from exc

        try:
            choices = completion.choices
            usage = completion.usage
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response is missing choices or usage",
            ) from exc
        if type(choices) is not list or not choices or usage is None:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response is missing choices or usage",
            )
        try:
            message = choices[0].message
            input_tokens = usage.prompt_tokens
            output_tokens = usage.completion_tokens
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response choice or usage fields are invalid",
            ) from exc
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = (
            getattr(usage, "cache_read_input_tokens", None)
            or (None if details is None else getattr(details, "cached_tokens", None))
            or 0
        )
        response = self._parse_response(message)
        if "".join(content_parts) != response.content:
            raise InvalidLLMResponse(
                "stream_content_mismatch",
                "streamed content does not match the assembled response",
            )
        return LLMCallResult(
            response=response,
            usage=LLMUsage(input_tokens, output_tokens, cached_tokens),
        )

    async def _completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> Any:
        return await self._router.acompletion(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None,
            stream=True,
            stream_options={"include_usage": True},
        )

    @staticmethod
    def _content_delta(chunk: Any) -> str:
        try:
            choices = chunk.choices
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream chunk is missing choices",
            ) from exc
        if type(choices) is not list:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream chunk choices must be an array",
            )
        if not choices:
            return ""
        try:
            content = choices[0].delta.content
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream choice delta is invalid",
            ) from exc
        if content is None:
            return ""
        if type(content) is not str:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream content delta must be str|null",
            )
        return content

    @staticmethod
    def _reasoning_delta(chunk: Any) -> str:
        try:
            choices = chunk.choices
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream chunk is missing choices",
            ) from exc
        if type(choices) is not list:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream chunk choices must be an array",
            )
        if not choices:
            return ""
        try:
            delta = choices[0].delta
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream choice delta is invalid",
            ) from exc
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning is None:
            return ""
        if type(reasoning) is not str:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model stream reasoning delta must be str|null",
            )
        return reasoning

    def _parse_response(self, message: Any) -> LLMResponse:
        try:
            data = message.model_dump(exclude_none=True)
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response message is invalid",
            ) from exc
        if type(data) is not dict:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response message must be an object",
            )
        raw_content = data.get("content")
        if raw_content is None:
            content = ""
        elif type(raw_content) is str:
            content = raw_content
        else:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response content must be str|null",
            )
        raw_calls = data.get("tool_calls")
        if raw_calls is None:
            calls = ()
        elif type(raw_calls) is list:
            try:
                calls = tuple(
                    ToolCall(
                        id=call["id"],
                        name=call["function"]["name"],
                        arguments=call["function"]["arguments"],
                    )
                    for call in raw_calls
                )
            except (KeyError, TypeError) as exc:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "model tool call fields are invalid",
                ) from exc
        else:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response tool_calls must be array|null",
            )
        extensions = {
            key: value
            for key, value in data.items()
            if key not in _NORMALIZED_MESSAGE_FIELDS and value is not None
        }
        return LLMResponse(content, calls, extensions)
