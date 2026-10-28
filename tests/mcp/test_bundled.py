import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from mcp.types import Tool

from konden.llm.config import initial_connections
from konden.mcp.approval import McpInstallProposalInput
from konden.mcp.bundled import BROWSER_SERVER_IDS, BUILTIN_SERVER_IDS
from konden.mcp.client_manager import McpClientError
from konden.mcp.composition import build_mcp
from konden.mcp.errors import McpConfigurationError
from konden.mcp.toolsets import ToolsetLoadError
from konden.paths import KondenHome


def test_browser_catalog_is_available_without_installation_or_connection(tmp_path):
    home = KondenHome(tmp_path)
    assembly = build_mcp(home)
    provider = assembly.toolset_provider
    assert {item.id for item in provider.descriptors()} == {
        "mcp:builtin-chrome-devtools", "mcp:builtin-playwright", "mcp:builtin-exa",
    }
    assert [item.id for item in provider.descriptors(read_only_only=True)] == ["mcp:builtin-exa"]
    assert assembly.service.registry.snapshot() == ()
    assert assembly.client_manager._connections == {}


def test_deepseek_catalog_tracks_the_existing_connection_without_installation_or_restart(tmp_path):
    home = KondenHome(tmp_path)
    assembly = build_mcp(home)
    provider = assembly.toolset_provider
    settings = initial_connections()
    settings["deepseek"]["api_key"] = "private-deepseek-key"
    home.connections_path.write_text(json.dumps(settings), encoding="utf-8")
    descriptors = provider.descriptors(read_only_only=True)
    assert {item.id for item in descriptors} == {"mcp:builtin-exa", "mcp:builtin-deepseek"}
    assert "private-deepseek-key" not in repr(descriptors)
    settings["deepseek"]["api_key"] = ""
    home.connections_path.write_text(json.dumps(settings), encoding="utf-8")
    assert [item.id for item in provider.descriptors(read_only_only=True)] == ["mcp:builtin-exa"]
    assert assembly.service.registry.snapshot() == ()
    assert not (home.mcp_root / "secrets").exists()
    assert assembly.client_manager._connections == {}


class BundledBrowserTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.assembly = build_mcp(KondenHome(Path(temporary.name)))

    async def asyncTearDown(self):
        await self.assembly.client_manager.aclose()

    async def test_bundled_connection_failure_points_to_product_preparation(self):
        assembly = self.assembly
        assembly.client_manager.list_tools = AsyncMock(side_effect=McpClientError("node missing"))
        for server_id in sorted(BROWSER_SERVER_IDS):
            with self.subTest(server_id=server_id):
                with self.assertRaises(ToolsetLoadError) as caught:
                    await assembly.toolset_provider.discover_tools(f"mcp:{server_id}")
                self.assertEqual(caught.exception.code, "MCP_TRANSPORT_ERROR")
                self.assertIn("node missing", caught.exception.message)
                self.assertIn("setup.ps1", caught.exception.hint)
                self.assertNotIn("propose_mcp", caught.exception.hint)
                self.assertEqual(assembly.service.registry.snapshot(), ())

    async def test_restored_browser_tool_keeps_disconnect_outcome_unknown(self):
        manager = self.assembly.client_manager
        provider = self.assembly.toolset_provider
        for server_id in sorted(BROWSER_SERVER_IDS):
            with self.subTest(server_id=server_id):
                manager.list_tools = AsyncMock(return_value=(
                    Tool(name="navigate", inputSchema={"type": "object", "properties": {}}),
                ))
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
                self.assertIsNone(result["ok"])
                self.assertEqual(result["data"]["execution_status"], "unknown")
                self.assertIn("先查询", result["hint"])
                self.assertIn("setup.ps1", result["hint"])
                self.assertIn("connection closed", result["error"])

    async def test_external_install_cannot_replace_bundled_server(self):
        for server_id in sorted(BUILTIN_SERVER_IDS):
            with self.subTest(server_id=server_id):
                with self.assertRaisesRegex(McpConfigurationError, "产品内置能力"):
                    await self.assembly.service.upsert_server(
                        server_id=server_id, display_name="external", transport="stdio",
                        transport_config={"command": "external"},
                    )
                with self.assertRaisesRegex(ValueError, "产品内置能力"):
                    McpInstallProposalInput(
                        server_id=server_id, display_name="external", transport="stdio",
                        command="external", source="user_input",
                    )
                self.assertEqual(self.assembly.service.registry.snapshot(), ())

    async def test_search_connection_failure_offers_browser_instead_of_reinstall(self):
        self.assembly.client_manager.list_tools = AsyncMock(side_effect=McpClientError("HTTP 429"))
        with self.assertRaises(ToolsetLoadError) as caught:
            await self.assembly.toolset_provider.discover_tools("mcp:builtin-exa")
        self.assertIn("web-research", caught.exception.hint)
        self.assertIn("浏览器", caught.exception.hint)
        self.assertNotIn("setup.ps1", caught.exception.hint)
