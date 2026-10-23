from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool

from redpanda.mcp.approval import McpInstallProposalInput
from redpanda.mcp.bundled import BROWSER_SERVER_IDS
from redpanda.mcp.client_manager import McpClientError
from redpanda.mcp.composition import build_mcp
from redpanda.mcp.errors import McpConfigurationError
from redpanda.mcp.toolsets import ToolsetLoadError
from redpanda.paths import RedPandaHome


def test_browser_catalog_is_available_without_installation_or_connection(tmp_path):
    home = RedPandaHome(tmp_path)
    assembly = build_mcp(home)
    provider = assembly.toolset_provider
    assert {item.id for item in provider.descriptors()} == {
        "mcp:builtin-chrome-devtools", "mcp:builtin-playwright",
    }
    assert provider.descriptors(read_only_only=True) == ()
    assert assembly.service.registry.snapshot() == ()
    assert assembly.client_manager._connections == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("server_id", sorted(BROWSER_SERVER_IDS))
async def test_bundled_connection_failure_points_to_product_preparation(tmp_path, server_id):
    assembly = build_mcp(RedPandaHome(tmp_path))
    assembly.client_manager.list_tools = AsyncMock(side_effect=McpClientError("node missing"))
    with pytest.raises(ToolsetLoadError) as caught:
        await assembly.toolset_provider.discover_tools(f"mcp:{server_id}")
    assert caught.value.code == "MCP_TRANSPORT_ERROR"
    assert "node missing" in caught.value.message
    assert "setup.ps1" in caught.value.hint
    assert "propose_mcp" not in caught.value.hint
    assert assembly.service.registry.snapshot() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("server_id", sorted(BROWSER_SERVER_IDS))
async def test_restored_browser_tool_keeps_disconnect_outcome_unknown(tmp_path, server_id):
    assembly = build_mcp(RedPandaHome(tmp_path))
    manager = assembly.client_manager
    manager.list_tools = AsyncMock(return_value=(
        Tool(name="navigate", inputSchema={"type": "object", "properties": {}}),
    ))
    provider = assembly.toolset_provider
    (discovered,) = await provider.discover_tools(f"mcp:{server_id}")
    manager.list_tools.reset_mock()
    restored = provider.restore_spec(
        toolset_id=f"mcp:{server_id}", revision=provider.descriptors()[0].revision,
        name=discovered.spec.name, description=discovered.spec.description,
        parameters=discovered.spec.parameters.schema(), requires_authorization=False,
        provider_data=discovered.provider_data,
    )
    manager.list_tools.assert_not_awaited()
    manager.call_tool = AsyncMock(side_effect=McpClientError("connection closed"))
    result = await restored.handler({})
    assert result["ok"] is None
    assert result["data"]["execution_status"] == "unknown"
    assert "先查询" in result["hint"]
    assert "setup.ps1" in result["hint"]
    assert "connection closed" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("server_id", sorted(BROWSER_SERVER_IDS))
async def test_external_install_cannot_replace_bundled_browser(tmp_path, server_id):
    assembly = build_mcp(RedPandaHome(tmp_path))
    with pytest.raises(McpConfigurationError, match="内置浏览器"):
        await assembly.service.upsert_server(
            server_id=server_id, display_name="external", transport="stdio",
            transport_config={"command": "external"},
        )
    with pytest.raises(ValueError, match="内置浏览器"):
        McpInstallProposalInput(
            server_id=server_id, display_name="external", transport="stdio",
            command="external", source="user_input",
        )
    assert assembly.service.registry.snapshot() == ()
