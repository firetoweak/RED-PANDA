"""证明只填现有连接文件就能启动 stdio 检索服务，重建装配后仍可调用。"""
import asyncio
import json

import pytest

from konden.llm.config import initial_connections
from konden.mcp.bundled import DEEPSEEK_SERVER_ID, OPENAI_SERVER_ID
from konden.mcp.composition import build_mcp
from konden.paths import KondenHome
from konden.search import SearchSettings, SearchConfig


pytestmark = pytest.mark.process


@pytest.mark.parametrize("provider_name", ["deepseek", "openai"])
def test_configured_search_is_available_without_registration_or_a_second_secret_copy(tmp_path, provider_name):
    home = KondenHome(tmp_path / "home")
    home.initialize()
    connections = initial_connections()
    connections[provider_name]["api_key"] = "test-provider-secret"
    original = json.dumps(connections)
    home.connections_path.write_text(original, encoding="utf-8")
    if provider_name == "openai":
        SearchSettings(home.connections_path).save(SearchConfig(provider="openai", openai_model="openai/gpt-6.1-terra"))
    server_id = DEEPSEEK_SERVER_ID if provider_name == "deepseek" else OPENAI_SERVER_ID

    async def scenario():
        assembly = build_mcp(home)
        provider = assembly.toolset_provider
        async with assembly.client_manager:
            discovered = await provider.discover_tools(f"mcp:{server_id}")
            assert {tool.provider_data["tool_name"] for tool in discovered} == (
                {"web_search", "web_fetch"} if provider_name == "deepseek" else {"web_search"})
        reopened = build_mcp(home)
        async with reopened.client_manager:
            descriptor = next(item for item in reopened.toolset_provider.descriptors(read_only_only=True)
                              if item.id == f"mcp:{server_id}")
            tool = next(item for item in discovered if item.provider_data["tool_name"] == (
                "web_fetch" if provider_name == "deepseek" else "web_search"))
            restored = reopened.toolset_provider.restore_spec(
                toolset_id=descriptor.id, revision=descriptor.revision,
                name=tool.spec.name, description=tool.spec.description,
                parameters=tool.spec.parameters.schema(), requires_authorization=False,
                provider_data=tool.provider_data,
            )
            # Invalid URL proves transport and restoration without a paid request.
            result = await restored.handler({"url": "file:///private.txt"} if provider_name == "deepseek" else {"query": ""})
            assert not result["ok"]
            assert ("HTTP/HTTPS" if provider_name == "deepseek" else "at least 1") in result["error"]
            assert reopened.service.registry.snapshot() == ()
            assert not (home.mcp_root / "secrets").exists()

    asyncio.run(scenario())
    assert home.connections_path.read_text(encoding="utf-8") == original
