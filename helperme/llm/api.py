from __future__ import annotations

from typing import Protocol

from thinllm import (
    ContentDeltaSink,
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

from helperme.llm.codec import (
    LLMRemoteError,
    decode_llm_error,
    decode_llm_result,
    encode_llm_error,
    encode_llm_result,
)
from helperme.llm.images import encode_images


__all__ = [
    "ContentDeltaSink",
    "InvalidLLMResponse",
    "LLMApi",
    "LLMAuthenticationError",
    "LLMCallResult",
    "LLMContextLengthError",
    "LLMProviderError",
    "LLMRemoteError",
    "LLMResponse",
    "LLMTransientError",
    "LLMUsage",
    "ToolCall",
    "decode_llm_error",
    "decode_llm_result",
    "encode_images",
    "encode_llm_error",
    "encode_llm_result",
]


class LLMApi(Protocol):
    """Assistant 使用的最小模型调用协议。

    content 为文本或有序内容块；图片块为
    {"type": "image", "id": 内容寻址 id, "mime": MIME 类型}。
    调用方在请求边界读取附件字节并编码；共享实现不持有 Session 附件闭包。
    """

    async def chat(
        self,
        messages: list[dict[str, object]],
        model: str,
        tools: list[dict[str, object]] | None = None,
        *,
        on_content_delta: ContentDeltaSink | None = None,
        on_reasoning_delta: ContentDeltaSink | None = None,
    ) -> LLMCallResult:
        ...
