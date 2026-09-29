from __future__ import annotations

import unittest

from helperme.assistant.failures import assistant_failure_message
from helperme.llm.api import (
    InvalidLLMResponse,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)


class ModelFailureMessageTest(unittest.TestCase):
    def test_authentication_error_points_to_gateway_configuration(self):
        message = assistant_failure_message(LLMAuthenticationError("401"))

        self.assertIn("模型认证失败", message)
        self.assertIn("FERRO_MASTER_KEY", message)

    def test_transient_error_reports_service_availability(self):
        message = assistant_failure_message(LLMTransientError("timeout"))

        self.assertIn("暂时不可用", message)
        self.assertIn("timeout", message)

    def test_other_known_model_errors_preserve_provider_detail(self):
        context = assistant_failure_message(LLMContextLengthError("too long"))
        provider = assistant_failure_message(LLMProviderError("bad model"))

        self.assertIn("too long", context)
        self.assertIn("bad model", provider)

    def test_empty_model_response_is_a_recognised_model_failure(self):
        message = assistant_failure_message(
            InvalidLLMResponse(
                "empty_model_response",
                "model response contains neither tool calls nor non-empty text",
            )
        )

        self.assertIn("没有给出可用回复或工具调用", message)
        self.assertIn("同一触发重试", message)

    def test_other_invalid_llm_response_preserves_detail(self):
        message = assistant_failure_message(
            InvalidLLMResponse("invalid_tool_arguments", "tool lookup arguments are not valid JSON")
        )

        self.assertIn("不符合约定", message)
        self.assertIn("not valid JSON", message)

    def test_unknown_internal_error_is_not_presented_as_model_failure(self):
        self.assertIsNone(assistant_failure_message(AssertionError("bug")))
