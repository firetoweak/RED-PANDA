"""独立于会话运行的模型连接测试。"""
from __future__ import annotations

from time import perf_counter

from redpanda.assistant.failures import assistant_failure_message
from redpanda.llm.api import (
    InvalidLLMResponse,
    LLMApi,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)


async def check_model_connection(llm: LLMApi, model: str) -> dict:
    started = perf_counter()
    ok, message = True, "模型可用"
    try:
        await llm.chat(
            [{"role": "user", "content": "Reply with OK only."}], model, tools=None,
        )
    except (
        LLMAuthenticationError, LLMContextLengthError, LLMProviderError,
        LLMTransientError, InvalidLLMResponse,
    ) as error:
        ok = False
        message = (f"模型响应不符合约定：{error}" if isinstance(error, InvalidLLMResponse)
                   else assistant_failure_message(error))
    return {"model": model, "ok": ok, "message": message,
            "elapsed_ms": round((perf_counter() - started) * 1000)}
