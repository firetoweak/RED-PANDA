"""Thin streaming clients for built-in model providers.

Stays thin: no cross-provider fallback, load balancing, circuit breaking,
cost tracking, or user-defined providers.
"""

from thinllm.chat_completions import ChatCompletionsClient
from thinllm.catalog import list_codex_models, list_models
from thinllm.codex import CodexResponsesClient
from thinllm.responses import ResponsesClient
from thinllm.errors import (
    InvalidLLMResponse,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
    MissingProviderSetting,
)
from thinllm.providers import (
    PROVIDERS,
    Endpoint,
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
    "CodexResponsesClient",
    "ResponsesClient",
    "create_client",
    "list_codex_models",
    "list_models",
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


def create_client(endpoint, **kwargs):
    """Select the built-in wire protocol without involving product code."""
    clients = {"openai": ResponsesClient, "openai-codex": CodexResponsesClient}
    return clients.get(endpoint.provider, ChatCompletionsClient)(endpoint, **kwargs)
