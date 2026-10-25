"""Wrong app paths or broken bundle links must fail before shipping browser tools."""
import asyncio
from dataclasses import replace
import re

import pytest

from konden.mcp.bundled import browser_servers
from konden.mcp.client_manager import McpClientManager
from konden.mcp.secrets import McpSecretStore
from konden.paths import KondenHome


pytestmark = pytest.mark.process


@pytest.mark.parametrize("server_id,open_tool,evaluate_tool", [
    ("builtin-chrome-devtools", "new_page", "evaluate_script"),
    ("builtin-playwright", "browser_navigate", "browser_evaluate"),
])
def test_bundled_browser_launches_and_evaluates_page(tmp_path, server_id, open_tool, evaluate_tool):
    async def scenario():
        home = KondenHome(tmp_path / "home")
        manager = McpClientManager(McpSecretStore.from_home(home), runtime_root=tmp_path / "runtime")
        record = next(server for server in browser_servers(tmp_path) if server.id == server_id)
        browser_log = tmp_path / "chrome-devtools.log"
        if server_id == "builtin-chrome-devtools":
            record = replace(record, transport_config=replace(
                record.transport_config,
                args=(*record.transport_config.args, f"--log-file={browser_log}"),
            ))
        try:
            async with asyncio.timeout(90):
                names = {tool.name for tool in await manager.list_tools(record)}
                assert {open_tool, evaluate_tool} <= names
                opened = await manager.call_tool(record, open_tool, {"url": "about:blank"})
                assert not opened.is_error, (
                    opened, browser_log.read_text(encoding="utf-8", errors="replace")
                    if server_id == "builtin-chrome-devtools" else "",
                )
                arguments = {
                    "function": "() => { document.title = 'konden-browser-ready'; return document.title; }",
                }
                if server_id == "builtin-chrome-devtools":
                    pages = "\n".join(block.text for block in opened.content if block.type == "text")
                    selected = re.search(r"(?m)^(\d+):.*\[selected\]", pages)
                    assert selected is not None, pages
                    arguments["pageId"] = int(selected[1])
                evaluated = await manager.call_tool(record, evaluate_tool, arguments)
                assert not evaluated.is_error, evaluated
                assert "konden-browser-ready" in evaluated.model_dump_json()
        finally:
            await manager.aclose()
    asyncio.run(scenario())
