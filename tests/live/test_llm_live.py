from __future__ import annotations

import os
import unittest

import pytest

from helperme.config import load_app_config
from helperme.llm.adapter import LiteLLMAdapter

pytestmark = pytest.mark.live


@unittest.skipUnless(
    os.environ.get("HELPERME_RUN_LIVE_TESTS") == "1",
    "设置 HELPERME_RUN_LIVE_TESTS=1 后显式运行 live 测试",
)
class LlmLiveClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_roundtrip(self):
        config = load_app_config()
        async with LiteLLMAdapter(config.model) as client:
            result = await client.chat(
                [{"role": "user", "content": "Reply with the single digit 2."}],
                config.model.active,
            )
        self.assertTrue(
            result.response.content.strip() or result.response.calls
        )
        self.assertGreaterEqual(result.usage.total_tokens, 1)

    @unittest.skipUnless(
        os.environ.get("HELPERME_RUN_DEEPSEEK_LIVE_TESTS") == "1",
        "设置 HELPERME_RUN_DEEPSEEK_LIVE_TESTS=1 后运行 DeepSeek 协议回传测试",
    )
    async def test_thinking_tool_roundtrip(self):
        config = load_app_config()
        tools = [{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Return the requested value.",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                    "additionalProperties": False,
                },
            },
        }]
        async with LiteLLMAdapter(config.model) as client:
            first = await client.chat(
                [{"role": "user", "content": "调用 lookup 查询 key=answer。"}],
                config.model.active,
                tools,
            )
            self.assertTrue(first.response.calls)
            assistant = {
                **first.response.message_extensions,
                "role": "assistant",
                "content": first.response.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in first.response.calls
                ],
            }
            second = await client.chat(
                [
                    {"role": "user", "content": "调用 lookup 查询 key=answer。"},
                    assistant,
                    {
                        "role": "tool",
                        "tool_call_id": first.response.calls[0].id,
                        "content": '{"answer":42}',
                    },
                ],
                config.model.active,
                tools,
            )
        self.assertTrue(second.response.content.strip() or second.response.calls)
