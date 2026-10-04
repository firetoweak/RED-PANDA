"""Serialize LLM results and errors across the Host/Worker process boundary."""

from __future__ import annotations

import builtins
import traceback

from thinllm import (
    InvalidLLMResponse,
    LLMAuthenticationError,
    LLMCallResult,
    LLMContextLengthError,
    LLMProviderError,
    LLMResponse,
    LLMTransientError,
    LLMUsage,
    ToolCall,
)


class LLMRemoteError(RuntimeError):
    """An exception that cannot be reconstituted without importing foreign types."""

    def __init__(self, exception_type: str, message: str, remote_traceback: str) -> None:
        super().__init__(f"{exception_type}: {message}\n{remote_traceback}")
        self.exception_type = exception_type
        self.original_message = message
        self.remote_traceback = remote_traceback


def encode_llm_result(result: LLMCallResult) -> dict[str, object]:
    return {
        "content": result.response.content,
        "calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in result.response.calls
        ],
        "message_extensions": result.response.message_extensions,
        "input_tokens": result.usage.input_tokens,
        "output_tokens": result.usage.output_tokens,
        "cached_input_tokens": result.usage.cached_input_tokens,
    }


def decode_llm_result(payload: dict[str, object]) -> LLMCallResult:
    calls = tuple(
        ToolCall(call["id"], call["name"], call["arguments"])
        for call in payload["calls"]
    )
    return LLMCallResult(
        LLMResponse(
            content=payload["content"],
            calls=calls,
            message_extensions=payload["message_extensions"],
        ),
        LLMUsage(
            payload["input_tokens"],
            payload["output_tokens"],
            payload["cached_input_tokens"],
        ),
    )


def encode_llm_error(error: BaseException) -> dict[str, object]:
    payload: dict[str, object] = {
        "exception_type": f"{type(error).__module__}.{type(error).__qualname__}",
        "message": str(error),
        "traceback": "".join(traceback.format_exception(error)),
    }
    if isinstance(error, InvalidLLMResponse):
        payload["code"] = error.code
    return payload


def decode_llm_error(payload: dict[str, object]) -> BaseException:
    name = payload["exception_type"]
    message = payload["message"]
    known = {
        "thinllm.errors.LLMTransientError": LLMTransientError,
        "thinllm.errors.LLMContextLengthError": LLMContextLengthError,
        "thinllm.errors.LLMProviderError": LLMProviderError,
        "thinllm.errors.LLMAuthenticationError": LLMAuthenticationError,
        "redpanda.llm.codec.LLMRemoteError": LLMRemoteError,
    }
    if name == "thinllm.errors.InvalidLLMResponse":
        return InvalidLLMResponse(payload["code"], message)
    cls = known.get(name)
    if cls is LLMRemoteError:
        return LLMRemoteError(name, message, payload["traceback"])
    if cls is not None:
        return cls(message)
    _, _, qualifier = name.rpartition(".")
    builtin = getattr(builtins, qualifier, None) if name.startswith("builtins.") else None
    if isinstance(builtin, type) and issubclass(builtin, Exception):
        return builtin(message)
    return LLMRemoteError(name, message, payload["traceback"])
