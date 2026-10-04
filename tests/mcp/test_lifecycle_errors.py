import asyncio
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx2

from redpanda.mcp.client_manager import (
    ManagedMcpConnection, McpClientManager, McpSdkError, _SdkConnectionOwner,
)
from redpanda.mcp.models import RuntimeAvailability
from redpanda.mcp.toolset_provider import McpToolsetProvider
from tests.mcp.test_mcp import _stdio_record


class McpLifecycleErrorsTest(unittest.IsolatedAsyncioTestCase):
    def manager(self, record, close_error):
        self.closes = 0
        self.opens = 0

        async def factory(record, secrets):
            self.opens += 1
            stack = AsyncExitStack()

            async def close():
                self.closes += 1
                raise close_error

            stack.push_async_callback(close)
            session = SimpleNamespace(
                protocol_version="test", server_capabilities=None,
                call_tool=AsyncMock(side_effect=McpSdkError("call offline")),
            )
            return ManagedMcpConnection(session, stack, record)

        return McpClientManager(
            Mock(resolve_many=Mock(return_value={})),
            runtime_root=Path.cwd(), session_factory=factory,
        )

    async def test_call_failure_survives_nested_network_close_failure(self):
        record = _stdio_record("offline")
        manager = self.manager(record, ExceptionGroup(
            "outer", [ExceptionGroup("inner", [httpx2.ConnectError("TLS")])]
        ))
        provider = McpToolsetProvider(
            SimpleNamespace(get=AsyncMock(return_value=record)), manager,
        )
        handler = provider._make_handler(
            record_id=record.id, expected_revision=record.revision,
            tool_name="search", output_validator=None,
        )
        result = await handler({})
        self.assertEqual(result["code"], "MCP_TRANSPORT_ERROR")
        self.assertEqual(result["error"], "call offline")
        self.assertEqual(self.closes, 1)
        self.assertEqual(self.opens, 1)  # No automatic retry.
        self.assertEqual(manager.runtime_state(record.id).status,
                         RuntimeAvailability.UNAVAILABLE)
        await manager.aclose()
        self.assertEqual(self.closes, 1)

    async def test_manager_shutdown_records_known_close_failure(self):
        record = _stdio_record("shutdown")
        manager = self.manager(record, httpx2.ConnectError("TLS"))
        await manager._ensure_connection(record)
        await manager.aclose()
        self.assertIn("TLS", manager.runtime_state(record.id).last_error_summary)

    async def test_open_and_cleanup_network_errors_are_converted(self):
        async def open_facade(owner, stack):
            stack.push_async_callback(AsyncMock(side_effect=httpx2.ConnectError("close TLS")))
            raise httpx2.ConnectError("open TLS")

        record = _stdio_record("opening")
        manager = McpClientManager(
            Mock(resolve_many=Mock(return_value={})), runtime_root=Path.cwd(),
        )
        # Avoid filesystem setup: the owner does not need a real stdio transport.
        from dataclasses import replace
        record = replace(record, transport_config=replace(record.transport_config, cwd=str(Path.cwd())))
        with patch.object(_SdkConnectionOwner, "_open_facade", open_facade):
            with self.assertRaises(McpSdkError) as caught:
                await manager._ensure_connection(record)
        self.assertIn("open TLS", str(caught.exception))
        self.assertIn("close TLS", str(caught.exception))
        await manager.aclose()

    async def test_mixed_or_cancelled_close_group_passes_through(self):
        for other in (RuntimeError("bug"), asyncio.CancelledError()):
            with self.subTest(other=type(other).__name__):
                error = BaseExceptionGroup("mixed", [httpx2.ConnectError("TLS"), other])
                record = _stdio_record("mixed")
                manager = self.manager(record, error)
                await manager._ensure_connection(record)
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await manager.invalidate(record.id)
                self.assertIs(caught.exception, error)

    async def test_owner_keeps_internal_error_when_close_also_fails(self):
        internal = RuntimeError("owner bug")
        network = httpx2.ConnectError("close TLS")

        async def open_facade(owner, stack):
            stack.push_async_callback(AsyncMock(side_effect=network))
            return SimpleNamespace(
                protocol_version="test", server_capabilities=None,
                call_tool=AsyncMock(side_effect=internal),
            )

        with patch.object(_SdkConnectionOwner, "_open_facade", open_facade):
            owner = _SdkConnectionOwner(_stdio_record("owner"), {})
            connection = await owner.start()
            with self.assertRaises(RuntimeError) as caught:
                await connection.session.call_tool("search", {})
            self.assertIs(caught.exception, internal)
            with self.assertRaises(BaseExceptionGroup) as caught:
                await connection.aclose()
            self.assertEqual(caught.exception.exceptions, (internal, network))
