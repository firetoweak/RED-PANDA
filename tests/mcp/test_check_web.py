import unittest
from unittest.mock import AsyncMock, patch
from mcp.types import CallToolResult, TextContent
from konden.mcp.check_web import check_web
from konden.mcp.client_manager import McpClientError

class WebCheckTest(unittest.IsolatedAsyncioTestCase):
    async def test_reports_external_failure_or_empty_result_without_claiming_ready(self):
        good = CallToolResult(content=[TextContent(type='text', text='source content')])
        for replies, expected in (([good, good], 0), ([good, CallToolResult(content=[])], 2),
                                  ([CallToolResult(content=[], is_error=True), good], 2),
                                  (McpClientError('HTTP 429'), 2)):
            with self.subTest(replies=replies), patch('konden.mcp.check_web.McpClientManager') as factory:
                manager = factory.return_value.__aenter__.return_value
                manager.call_tool = AsyncMock(side_effect=replies)
                self.assertEqual(await check_web(), expected)
                factory.return_value.__aexit__.assert_awaited_once()
