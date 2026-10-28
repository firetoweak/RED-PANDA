"""证明配置命令能启动实际 stdio 服务，并使只读会话在重启后仍可发现和调用。"""
import asyncio
import json

import pytest

from konden.llm.config import initial_connections
from konden.mcp.composition import build_mcp
from konden.mcp.secrets import McpSecretStore
from konden.paths import KondenHome
from scripts.configure_deepseek_search import SERVER_ID, configure


pytestmark = pytest.mark.process


def test_configure_and_reopen_deepseek_tools_without_exposing_or_changing_model_credentials(tmp_path):
    home = KondenHome(tmp_path / "home")
    home.initialize()
    connections = initial_connections()
    connections["deepseek"]["api_key"] = "test-deepseek-secret"
    original = json.dumps(connections)
    home.connections_path.write_text(original, encoding="utf-8")

    async def scenario():
        assert await configure(home) == 0
        assembly = build_mcp(home)
        async with assembly.client_manager as manager:
            record = await assembly.service.registry.get(SERVER_ID)
            assert record.enabled and record.read_only
            assert "test-deepseek-secret" not in json.dumps(record.to_dict())
            assert McpSecretStore.from_home(home).resolve_many(record.credential_refs) == {
                "DEEPSEEK_API_KEY": "test-deepseek-secret",
            }
            assert record.toolset_id in {
                descriptor.id for descriptor in assembly.toolset_provider.descriptors(read_only_only=True)
            }
            tools = {tool.name: tool for tool in await manager.list_tools(record)}
            assert set(tools) == {"web_search", "web_fetch"}
            assert tools["web_search"].input_schema["properties"]["query"]["maxLength"] == 128
            # A schema error proves request/response transport without calling the paid API.
            result = await manager.call_tool(record, "web_fetch", {"url": "file:///private.txt"})
            assert result.is_error
            assert "HTTP/HTTPS" in result.model_dump_json()

    asyncio.run(scenario())
    assert home.connections_path.read_text(encoding="utf-8") == original
