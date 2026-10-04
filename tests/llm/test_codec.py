from __future__ import annotations

import unittest

from redpanda.llm.api import (
    InvalidLLMResponse,
    LLMRemoteError,
    LLMTransientError,
    decode_llm_error,
    decode_llm_result,
    encode_llm_error,
    encode_llm_result,
)
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall


class LlmCodecTest(unittest.TestCase):
    def test_roundtrip_result_preserves_calls_and_usage(self):
        result = LLMCallResult(
            LLMResponse(
                content="done",
                calls=(ToolCall("c1", "read_file", "{}"),),
                message_extensions={"reasoning_content": ""},
            ),
            LLMUsage(3, 2, 1),
        )
        restored = decode_llm_result(encode_llm_result(result))
        self.assertEqual(restored, result)

    def test_known_errors_reconstitute_their_types(self):
        cases = (
            LLMTransientError("timeout"),
            InvalidLLMResponse("empty_model_response", "blank"),
            RuntimeError("intentional worker crash"),
        )
        for error in cases:
            with self.subTest(type=type(error).__name__):
                restored = decode_llm_error(encode_llm_error(error))
                self.assertIs(type(restored), type(error))
                self.assertEqual(str(restored), str(error))
                if isinstance(error, InvalidLLMResponse):
                    self.assertEqual(restored.code, error.code)

    def test_foreign_errors_preserve_type_name_and_traceback(self):
        class ProviderBoom(RuntimeError):
            pass

        ProviderBoom.__module__ = "foreign_gateway.exceptions"
        ProviderBoom.__qualname__ = "APIError"
        try:
            raise ProviderBoom("upstream")
        except ProviderBoom as error:
            restored = decode_llm_error(encode_llm_error(error))
        self.assertIsInstance(restored, LLMRemoteError)
        self.assertEqual(
            restored.exception_type,
            "foreign_gateway.exceptions.APIError",
        )
        self.assertIn("upstream", restored.original_message)
        self.assertIn("ProviderBoom", restored.remote_traceback)
