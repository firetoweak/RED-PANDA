from __future__ import annotations

from konden.llm.api import (
    InvalidLLMResponse,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)


def assistant_failure_message(error: BaseException) -> str | None:
    if isinstance(error, LLMAuthenticationError):
        return (
            "模型认证失败：登录凭据无效，或当前账号无权访问配置的模型。"
            "请检查个人目录 connections.json 中的供应商设置；"
            "openai-codex 可运行 python -m thinllm auth codex --login 重新授权并更新连接配置。"
        )
    if isinstance(error, LLMTransientError):
        return f"模型服务暂时不可用：{error}"
    if isinstance(error, LLMContextLengthError):
        return f"模型输入超出上下文限制：{error}"
    if isinstance(error, LLMProviderError):
        return f"模型请求失败：{error}"
    if isinstance(error, InvalidLLMResponse):
        if error.code == "empty_model_response":
            return (
                "模型这一拍没有给出可用回复或工具调用。"
                "下一条消息会从同一触发重试。"
            )
        return f"模型响应不符合约定：{error}"
    return None
