"""Thin client for built-in OpenAI-compatible model providers.

Stays thin: no cross-provider fallback, load balancing, circuit breaking,
cost tracking, model catalog or discovery, and no user-defined providers.
"""

from thinllm.chat_completions import ChatCompletionsClient
from thinllm.errors import (
    InvalidLLMResponse,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)
from thinllm.providers import (
    PROVIDERS,
    Endpoint,
    MissingProviderSetting,
    resolve_endpoint,
)
from thinllm.types import (
    ContentDeltaSink,
    LLMCallResult,
    LLMResponse,
    LLMUsage,
    ToolCall,
)

__all__ = [
    "PROVIDERS",
    "ChatCompletionsClient",
    "ContentDeltaSink",
    "Endpoint",
    "InvalidLLMResponse",
    "LLMAuthenticationError",
    "LLMCallResult",
    "LLMContextLengthError",
    "LLMProviderError",
    "LLMResponse",
    "LLMTransientError",
    "LLMUsage",
    "MissingProviderSetting",
    "ToolCall",
    "resolve_endpoint",
]
