import unittest

from thinllm import InvalidLLMResponse, LLMResponse, LLMUsage, ToolCall


class LLMResponseContractTest(unittest.TestCase):
    def test_extensions_reject_core_fields_and_non_json_values(self):
        for extensions in (
            {"content": "shadow"},
            {"reasoning_content": object()},
            {"reasoning_content": float("nan")},
        ):
            with self.subTest(extensions=extensions):
                with self.assertRaises(InvalidLLMResponse):
                    LLMResponse(content="done", message_extensions=extensions)

    def test_text_response_requires_non_empty_content(self):
        for content in ("", "   "):
            with self.subTest(content=content):
                with self.assertRaises(InvalidLLMResponse):
                    LLMResponse(content=content)

    def test_tool_calls_may_have_empty_content(self):
        response = LLMResponse(
            content="",
            calls=(ToolCall("c1", "read_file", "{}"),),
        )
        self.assertEqual(response.calls[0].name, "read_file")

    def test_cached_input_tokens_cannot_exceed_input_tokens(self):
        with self.assertRaises(InvalidLLMResponse):
            LLMUsage(
                input_tokens=10,
                output_tokens=1,
                cached_input_tokens=11,
            )
