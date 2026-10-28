"""证明只填现有连接文件就能启动 stdio 检索服务，重建装配后仍可调用。"""
import asyncio
import json

import pytest

from konden.llm.config import initial_connections
from konden.mcp.bundled import DEEPSEEK_SERVER_ID
from konden.mcp.composition import build_mcp
from konden.paths import KondenHome


pytestmark = pytest.mark.process


def test_configured_deepseek_is_available_without_registration_or_a_second_secret_copy(tmp_path):
    home = KondenHome(tmp_path / "home")
    home.initialize()
    connections = initial_connections()
    connections["deepseek"]["api_key"] = "test-deepseek-secret"
    original = json.dumps(connections)
    home.connections_path.write_text(original, encoding="utf-8")

    async def scenario():
        assembly = build_mcp(home)
        provider = assembly.toolset_provider
        async with assembly.client_manager:
            discovered = await provider.discover_tools(f"mcp:{DEEPSEEK_SERVER_ID}")
            assert {tool.spec.name for tool in discovered} == {
                "mcp__builtin-deepseek__web_search", "mcp__builtin-deepseek__web_fetch",
            }
        reopened = build_mcp(home)
        async with reopened.client_manager:
            descriptor = next(item for item in reopened.toolset_provider.descriptors(read_only_only=True)
                              if item.id == f"mcp:{DEEPSEEK_SERVER_ID}")
            tool = next(item for item in discovered if item.provider_data["tool_name"] == "web_fetch")
            restored = reopened.toolset_provider.restore_spec(
                toolset_id=descriptor.id, revision=descriptor.revision,
                name=tool.spec.name, description=tool.spec.description,
                parameters=tool.spec.parameters.schema(), requires_authorization=False,
                provider_data=tool.provider_data,
            )
            # Invalid URL proves transport and restoration without a paid request.
            result = await restored.handler({"url": "file:///private.txt"})
            assert not result["ok"]
            assert "HTTP/HTTPS" in result["error"]
            assert reopened.service.registry.snapshot() == ()
            assert not (home.mcp_root / "secrets").exists()

    asyncio.run(scenario())
    assert home.connections_path.read_text(encoding="utf-8") == original
