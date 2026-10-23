from __future__ import annotations


class MissingProviderSetting(ValueError):
    def __init__(self, setting: str):
        self.setting = setting
        super().__init__(f"{setting} is required")


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
