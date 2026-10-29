from __future__ import annotations

import os
import unittest

import pytest

from konden.config import load_app_config
from thinllm import create_client
from konden.llm.config import load_endpoint

pytestmark = pytest.mark.live


@unittest.skipUnless(
    os.environ.get("KONDEN_RUN_LIVE_TESTS") == "1",
    "设置 KONDEN_RUN_LIVE_TESTS=1 后显式运行 live 测试",
)
class LlmLiveClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_roundtrip(self):
        config = load_app_config()
        async with create_client(load_endpoint(config.default)) as client:
            result = await client.chat(
                [{"role": "user", "content": "Reply with the single digit 2."}],
                config.default_model,
            )
        self.assertTrue(
            result.response.content.strip() or result.response.calls
        )
        self.assertGreaterEqual(result.usage.total_tokens, 1)

    async def test_tool_roundtrip_preserves_native_state(self):
        config = load_app_config()
        tools = [{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Return the requested value.",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["key"],
                    "additionalProperties": False,
                },
            },
        }]
        prompt = "调用 lookup 查询 key=answer，不要提供 note；收到结果后用一句话报告其中的数字。"
        async with create_client(load_endpoint(config.default)) as client:
            first = await client.chat(
                [{"role": "user", "content": prompt}],
                config.default_model,
                tools,
            )
            self.assertTrue(first.response.calls)
            assistant = {
                **first.response.message_extensions,
                "role": "assistant",
                "content": first.response.content or None,
                "tool_calls": [
                    {
                        "id": f"command-{index}",
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for index, call in enumerate(first.response.calls)
                ],
            }
            second = await client.chat(
                [
                    {"role": "user", "content": prompt},
                    assistant,
                    *[{
                        "role": "tool",
                        "tool_call_id": f"command-{index}",
                        "content": '{"answer":42}',
                    } for index, _ in enumerate(first.response.calls)],
                ],
                config.default_model,
                tools,
            )
        self.assertIn("42", second.response.content)
