import asyncio
import unittest
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx2

from redpanda.mcp.client_manager import (
    ManagedMcpConnection, McpClientManager, McpSdkError, _SdkConnectionOwner,
    _is_transport_failure,
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
        self.assertEqual(
            result["error"],
            "与 offline 的连接中断，这次调用没有完成：call offline",
        )
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

    async def test_internal_close_group_passes_through(self):
        error = BaseExceptionGroup(
            "mixed", [httpx2.ConnectError("TLS"), RuntimeError("bug")],
        )
        record = _stdio_record("mixed")
        manager = self.manager(record, error)
        await manager._ensure_connection(record)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await manager.invalidate(record.id)
        self.assertIs(caught.exception, error)

    async def test_close_cancellation_beside_network_error_is_a_tool_error(self):
        error = BaseExceptionGroup(
            "mixed",
            [httpx2.ConnectError("connection reset"), asyncio.CancelledError()],
        )
        record = replace(_stdio_record("tavily"), display_name="Tavily")
        stopped = ConnectionError("MCP connection owner 已停止: tavily")

        async def factory(server, secrets):
            self.closes = 0

            async def close():
                self.closes += 1
                raise error

            stack = AsyncExitStack()
            stack.push_async_callback(close)
            session = SimpleNamespace(
                protocol_version="test", server_capabilities=None,
                call_tool=AsyncMock(side_effect=stopped),
            )
            return ManagedMcpConnection(session, stack, server)

        manager = McpClientManager(
            Mock(resolve_many=Mock(return_value={})),
            runtime_root=Path.cwd(), session_factory=factory,
        )
        provider = McpToolsetProvider(
            SimpleNamespace(get=AsyncMock(return_value=record)), manager,
        )
        handler = provider._make_handler(
            record_id=record.id, expected_revision=record.revision,
            tool_name="search", output_validator=None,
        )
        result = await handler({})
        self.assertEqual(result["code"], "MCP_TRANSPORT_ERROR")
        self.assertEqual(result["error"], "与 Tavily 的连接中断，这次调用没有完成")
        self.assertNotIn("owner", result["error"])
        self.assertNotIn("CancelledError", result["error"])
        self.assertEqual(
            manager.runtime_state(record.id).status,
            RuntimeAvailability.UNAVAILABLE,
        )
        await manager.aclose()

    async def test_caller_cancellation_still_propagates(self):
        started = asyncio.Event()

        async def call_tool(name, arguments):
            started.set()
            await asyncio.Event().wait()

        async def open_facade(owner, stack):
            return SimpleNamespace(
                protocol_version="test", server_capabilities=None,
                call_tool=call_tool,
            )

        record = _stdio_record("tavily")
        with patch.object(_SdkConnectionOwner, "_open_facade", open_facade):
            owner = _SdkConnectionOwner(record, {})
            connection = await owner.start()
            task = asyncio.create_task(connection.session.call_tool("search", {}))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await connection.aclose()

    async def test_stream_reset_group_reaches_the_model_as_connection_loss(self):
        network = httpx2.ConnectError("connection reset")

        async def open_facade(owner, stack):
            async def close():
                raise asyncio.CancelledError()

            stack.push_async_callback(close)
            return SimpleNamespace(
                protocol_version="test", server_capabilities=None,
                call_tool=AsyncMock(side_effect=BaseExceptionGroup(
                    "stream closed", [network, asyncio.CancelledError()],
                )),
            )

        record = replace(_stdio_record("tavily"), display_name="Tavily")
        manager = McpClientManager(
            Mock(resolve_many=Mock(return_value={})), runtime_root=Path.cwd(),
        )
        provider = McpToolsetProvider(
            SimpleNamespace(get=AsyncMock(return_value=record)), manager,
        )
        handler = provider._make_handler(
            record_id=record.id, expected_revision=record.revision,
            tool_name="search", output_validator=None,
        )
        with patch.object(_SdkConnectionOwner, "_open_facade", open_facade):
            result = await handler({})
        self.assertEqual(result["code"], "MCP_TRANSPORT_ERROR")
        self.assertEqual(
            result["error"],
            "与 Tavily 的连接中断，这次调用没有完成：connection reset",
        )
        self.assertNotIn("owner", result["error"])
        self.assertNotIn("CancelledError", result["error"])
        self.assertNotIn("运行失败且关闭失败", result["error"])
        await manager.aclose()

    async def test_cancelling_task_does_not_treat_cleanup_as_transport_failure(self):
        group = BaseExceptionGroup(
            "mixed", [httpx2.ConnectError("TLS"), asyncio.CancelledError()],
        )
        seen: dict[str, bool] = {}

        async def body():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                seen["group"] = _is_transport_failure(group)
                seen["cancel"] = _is_transport_failure(asyncio.CancelledError())
                raise

        task = asyncio.create_task(body())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(seen["group"])
        self.assertFalse(seen["cancel"])
        self.assertTrue(_is_transport_failure(group))
        self.assertTrue(_is_transport_failure(asyncio.CancelledError()))

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
