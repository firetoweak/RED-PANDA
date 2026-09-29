from __future__ import annotations


class InvalidLLMResponse(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LLMTransientError(RuntimeError):
    pass


class LLMContextLengthError(RuntimeError):
    pass


class LLMProviderError(RuntimeError):
    pass


class LLMAuthenticationError(LLMProviderError):
    pass
