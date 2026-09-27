from __future__ import annotations

import unittest
import sys
import asyncio
import json
import socket
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from mcp.types import (
    CallToolResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    Prompt,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)

from helperme.paths import HelperMeHome
from helperme.tools.spec import JsonSchemaParameters
from helperme.mcp.toolsets import ToolsetLoadError
from helperme.assistant.artifacts import FileArtifactStore
from helperme.assistant.context.projection import externalize_payload
from helperme.mcp.adapter import (
    adapt_call_result,
    build_output_validator,
    encode_tool_name,
)
from helperme.mcp.application import McpApplicationService
from helperme.mcp.errors import McpRecoveryPreconditionError
from helperme.mcp.client_manager import (
    ManagedMcpConnection,
    McpClientManager,
    McpSdkError,
    _SdkConnectionOwner,
)
from helperme.mcp.composition import build_mcp
from helperme.mcp.models import (
    QUERY_SECRET_KEY,
    McpServerRecord,
    RuntimeAvailability,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    TransportKind,
)
from helperme.mcp.registry import McpRegistry
from helperme.mcp.secrets import McpSecretStore
from helperme.mcp.management_tools import create_mcp_management_specs


class FakeMcpSession:
    def __init__(
        self,
        *,
        tools: list[Tool] | None = None,
        call_results: dict[str, CallToolResult] | None = None,
        fail_initialize: Exception | None = None,
    ) -> None:
        self.tools = tools or []
        self.call_results = call_results or {}
        self.fail_initialize = fail_initialize
        self.initialized = False
        self.calls: list[tuple[str, dict | None]] = []

    @property
    def protocol_version(self) -> str:
        return "2026-07-28"

    @property
    def server_capabilities(self) -> Any:
        class _Caps:
            def model_dump(self, **kwargs):
                return {"tools": {}}

        return _Caps()

    async def list_tools(self, *, cursor=None) -> ListToolsResult:
        return ListToolsResult(tools=self.tools)

    async def call_tool(self, name: str, arguments=None) -> CallToolResult:
        self.calls.append((name, arguments))
        if name in self.call_results:
            return self.call_results[name]
        return CallToolResult(
            content=[TextContent(type="text", text=f"ok:{name}")],
            isError=False,
        )

    async def list_resources(self, *, cursor=None):
        return ListResourcesResult(resources=[])

    async def list_resource_templates(self, *, cursor=None):
        return ListResourceTemplatesResult(resourceTemplates=[])

    async def read_resource(self, uri):
        raise NotImplementedError

    async def list_prompts(self, *, cursor=None):
        return ListPromptsResult(prompts=[])

    async def get_prompt(self, name, arguments=None):
        raise NotImplementedError


def _tool(
    name: str,
    schema: dict | None = None,
    output_schema: dict | None = None,
) -> Tool:
    return Tool(
        name=name,
        description=f"tool {name}",
        inputSchema=schema
        or {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        outputSchema=output_schema,
    )


def _runtime_root(workspace: HelperMeHome) -> Path:
    return workspace.mcp_root / "runtime"


def _stdio_record(server_id: str) -> McpServerRecord:
    return McpServerRecord(
        id=server_id,
        display_name=server_id,
        description="",
        transport=TransportKind.STDIO,
        transport_config=StdioTransportConfig(command="python"),
        enabled=True,
    )


class McpSdkBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_owner_wraps_expected_external_open_error(self):
        with patch.object(
            _SdkConnectionOwner,
            "_open_facade",
            AsyncMock(side_effect=OSError("transport unavailable")),
        ):
            owner = _SdkConnectionOwner(_stdio_record("expected"), {})
            with self.assertRaisesRegex(
                McpSdkError,
                "transport unavailable",
            ) as caught:
                await owner.start()

        self.assertIsInstance(caught.exception.__cause__, OSError)

    async def test_owner_exposes_unexpected_internal_open_error(self):
        with patch.object(
            _SdkConnectionOwner,
            "_open_facade",
            AsyncMock(side_effect=RuntimeError("internal open bug")),
        ):
            owner = _SdkConnectionOwner(_stdio_record("broken-open"), {})
            with self.assertRaisesRegex(RuntimeError, "internal open bug"):
                await owner.start()

    async def test_owner_exposes_unexpected_internal_operation_error(self):
        async def broken_list_tools(**_kwargs):
            raise RuntimeError("internal operation bug")

        facade = SimpleNamespace(
            protocol_version="2026-07-28",
            server_capabilities=SimpleNamespace(),
            list_tools=broken_list_tools,
        )
        with patch.object(
            _SdkConnectionOwner,
            "_open_facade",
            AsyncMock(return_value=facade),
        ):
            owner = _SdkConnectionOwner(_stdio_record("broken-call"), {})
            connection = await owner.start()
            with self.assertRaisesRegex(RuntimeError, "internal operation bug"):
                await connection.session.list_tools()
            with self.assertRaisesRegex(RuntimeError, "internal operation bug"):
                await owner._task


class McpRegistrySecretTest(unittest.IsolatedAsyncioTestCase):
    async def test_registry_requires_current_version_and_complete_records(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            record = _stdio_record("demo").to_dict()

            registry.path.write_text(
                json.dumps({"version": 0, "servers": [record]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                await registry.list_servers()

            for field in (
                "enabled", "read_only", "last_status", "last_checked_at", "last_error_summary"
            ):
                with self.subTest(missing=field):
                    incomplete = dict(record)
                    del incomplete[field]
                    registry.path.write_text(
                        json.dumps({"version": 1, "servers": [incomplete]}),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, field):
                        await registry.list_servers()

            http_record = McpServerRecord(
                id="http-demo",
                display_name="HTTP Demo",
                description="",
                transport=TransportKind.STREAMABLE_HTTP,
                transport_config=StreamableHttpTransportConfig(
                    url="https://example.com/mcp"
                ),
            ).to_dict()
            del http_record["transport_config"]["query_refs"]
            registry.path.write_text(
                json.dumps({"version": 1, "servers": [http_record]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "query_refs"):
                await registry.list_servers()

    async def test_secret_store_requires_current_version_and_string_values(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            secrets = McpSecretStore.from_home(workspace)
            secret_root = workspace.mcp_root / "secrets"
            secret_root.mkdir(parents=True)
            path = secret_root / "demo.json"

            path.write_text(
                '{"version":0,"values":{"TOKEN":"value"}}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                secrets.snapshot_namespace("demo")

            path.write_text(
                '{"version":1,"values":{"TOKEN":123}}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                secrets.snapshot_namespace("demo")

    async def test_registry_and_secrets_roundtrip(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            refs = secrets.put_namespace("demo", {"TOKEN": "secret-value"})
            service = McpApplicationService(
                registry,
                secrets,
                McpClientManager(
                    secrets,
                    runtime_root=_runtime_root(workspace),
                ),
            )
            record = await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                description="demo server",
                transport="stdio",
                transport_config={
                    "command": "python",
                    "args": ["server.py"],
                    "cwd": str(Path(directory)),
                },
                secrets={"TOKEN": "secret-value"},
                enabled=False,
            )
            self.assertEqual(record.id, "demo")
            self.assertFalse(record.enabled)
            self.assertIn("TOKEN", record.transport_config.env_refs)
            self.assertNotIn(
                "secret-value",
                registry.path.read_text(encoding="utf-8"),
            )
            self.assertEqual(secrets.resolve(refs["TOKEN"]), "secret-value")

            enabled = await service.set_server_enabled("demo", True)
            self.assertTrue(enabled.enabled)
            self.assertEqual(enabled.revision, 2)

            await service.remove_server("demo")
            self.assertEqual(await registry.list_servers(), ())
            self.assertFalse(
                (workspace.mcp_root / "secrets" / "demo.json").exists()
            )

    async def test_remove_server_rejects_stale_revision(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            service = McpApplicationService(
                McpRegistry.from_home(workspace),
                McpSecretStore.from_home(workspace),
                McpClientManager(
                    McpSecretStore.from_home(workspace),
                    runtime_root=_runtime_root(workspace),
                ),
            )
            record = await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                transport="stdio",
                transport_config={
                    "command": "python",
                    "args": ["server.py"],
                },
            )

            with self.assertRaises(McpRecoveryPreconditionError):
                await service.remove_server(
                    "demo",
                    expected_revision=record.revision + 1,
                )
            self.assertIsNotNone(await service.registry.get("demo"))

            removed = await service.remove_server(
                "demo",
                expected_revision=record.revision,
            )
            self.assertEqual(removed.id, "demo")
            self.assertIsNone(await service.registry.get("demo"))

    async def test_streamable_http_url_query_is_extracted_as_secret(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            service = McpApplicationService(
                registry,
                secrets,
                McpClientManager(
                    secrets,
                    runtime_root=_runtime_root(workspace),
                ),
            )
            record = await service.upsert_server(
                server_id="tavily",
                display_name="Tavily",
                transport="streamable_http",
                transport_config={
                    "url": (
                        "https://mcp.tavily.com/mcp/"
                        "?tavilyApiKey=secret-value"
                    ),
                },
                enabled=False,
            )

            config = record.transport_config
            self.assertIsInstance(config, StreamableHttpTransportConfig)
            self.assertEqual(config.url, "https://mcp.tavily.com/mcp/")
            self.assertIn(QUERY_SECRET_KEY, config.query_refs)
            self.assertNotIn(
                "secret-value",
                registry.path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                secrets.snapshot_namespace("tavily"),
                {QUERY_SECRET_KEY: "tavilyApiKey=secret-value"},
            )
            self.assertEqual(
                record.credential_refs[QUERY_SECRET_KEY],
                secrets.ref_for("tavily", QUERY_SECRET_KEY),
            )

    async def test_streamable_http_query_refs_roundtrip(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            service = McpApplicationService(
                registry,
                secrets,
                McpClientManager(
                    secrets,
                    runtime_root=_runtime_root(workspace),
                ),
            )
            record = await service.upsert_server(
                server_id="tavily",
                display_name="Tavily",
                transport="streamable_http",
                transport_config={
                    "url": "https://mcp.tavily.com/mcp/?a=1&b=2",
                },
                enabled=False,
            )

            restored = McpServerRecord.from_dict(record.to_dict())

            self.assertEqual(
                restored.transport_config.url,
                record.transport_config.url,
            )
            self.assertEqual(
                restored.transport_config.query_refs,
                record.transport_config.query_refs,
            )
            self.assertEqual(restored.credential_refs, record.credential_refs)

    async def test_agent_management_tools_find_and_test_disabled_server(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
                session_factory=AsyncMock(),
            )
            service = McpApplicationService(registry, secrets, manager)
            await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                transport="stdio",
                transport_config={"command": "python", "args": []},
                enabled=False,
            )
            runtime = manager.runtime_state("demo")
            runtime.mark_available(
                negotiated_version="2026-07-28",
                capabilities={"tools": {}},
            )
            service.test_server = AsyncMock(return_value=runtime)
            specs = {spec.name: spec for spec in create_mcp_management_specs(service)}

            listed = await specs["list_mcp_servers"].handler(
                specs["list_mcp_servers"].parameters.validate({})
            )
            tested = await specs["test_mcp_server"].handler(
                specs["test_mcp_server"].parameters.validate({
                    "server_id": "demo",
                })
            )

            self.assertEqual(listed["data"]["servers"][0]["id"], "demo")
            self.assertFalse(listed["data"]["servers"][0]["enabled"])
            self.assertEqual(tested["code"], "MCP_SERVER_AVAILABLE")
            self.assertEqual(
                set(tested["data"]),
                {"server_id", "enabled", "revision", "runtime"},
            )

    async def test_test_and_enable_only_enables_available_server(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            record = await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                transport="stdio",
                transport_config={"command": "python", "args": []},
                enabled=False,
            )
            runtime = manager.runtime_state("demo")
            runtime.mark_available(
                negotiated_version="2026-07-28",
                capabilities={"tools": {}},
            )
            service._test_record = AsyncMock(return_value=runtime)

            activation = await service.test_and_enable(
                "demo",
                expected_revision=record.revision,
            )

            self.assertTrue(activation.succeeded)
            self.assertTrue(activation.record.enabled)
            self.assertEqual(activation.record.revision, 2)
            self.assertEqual(
                activation.record.last_status,
                RuntimeAvailability.AVAILABLE,
            )

    async def test_test_and_enable_preserves_disabled_on_failure(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            record = await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                transport="stdio",
                transport_config={"command": "python", "args": []},
                enabled=False,
            )
            runtime = manager.runtime_state("demo")
            runtime.mark_unavailable("connection failed")
            service._test_record = AsyncMock(return_value=runtime)

            activation = await service.test_and_enable(
                "demo",
                expected_revision=record.revision,
            )

            self.assertFalse(activation.succeeded)
            self.assertFalse(activation.record.enabled)
            self.assertEqual(activation.record.revision, 1)
            self.assertEqual(
                activation.record.last_status,
                RuntimeAvailability.UNAVAILABLE,
            )

    async def test_test_server_persists_last_status(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            record = await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                transport="stdio",
                transport_config={"command": "python", "args": []},
                enabled=False,
            )
            self.assertEqual(record.last_status, RuntimeAvailability.UNKNOWN)

            runtime = manager.runtime_state("demo")
            runtime.mark_available(
                negotiated_version="2026-07-28",
                capabilities={"tools": {}},
            )
            service._test_record = AsyncMock(return_value=runtime)

            await service.test_server("demo")

            stored = await registry.get("demo")
            self.assertIsNotNone(stored)
            self.assertEqual(
                stored.last_status,
                RuntimeAvailability.AVAILABLE,
            )
            self.assertEqual(stored.revision, 1)

    async def test_mark_tested_persists_and_survives_reload(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            service = McpApplicationService(
                registry,
                secrets,
                McpClientManager(
                    secrets,
                    runtime_root=_runtime_root(workspace),
                ),
            )
            await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                transport="stdio",
                transport_config={"command": "python", "args": []},
                enabled=False,
            )

            await registry.mark_tested(
                "demo",
                RuntimeAvailability.UNAVAILABLE,
                error_summary="connection failed",
            )

            reloaded_registry = McpRegistry.from_home(workspace)
            reloaded = await reloaded_registry.get("demo")
            self.assertIsNotNone(reloaded)
            self.assertEqual(
                reloaded.last_status,
                RuntimeAvailability.UNAVAILABLE,
            )
            self.assertEqual(reloaded.last_error_summary, "connection failed")
            self.assertIsNotNone(reloaded.last_checked_at)
            self.assertEqual(reloaded.revision, 1)

    async def test_assembly_exposes_agent_management_and_recovery_specs(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()

            mcp = build_mcp(workspace)

            self.assertEqual(
                {spec.name for spec in mcp.management_specs},
                {"list_mcp_servers", "test_mcp_server"},
            )
            self.assertEqual(
                {operation.name for operation in mcp.control_operations},
                {
                    "propose_mcp_install",
                    "propose_mcp_recovery",
                    "propose_mcp_update",
                    "propose_mcp_remove",
                },
            )

    async def test_http_non_localhost_requires_https(self):
        with self.assertRaises(ValueError):
            StreamableHttpTransportConfig(url="http://example.com/mcp")

    async def test_server_id_rejects_secret_ref_and_path_ambiguity(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            service = McpApplicationService(
                McpRegistry.from_home(workspace),
                McpSecretStore.from_home(workspace),
                McpClientManager(
                    McpSecretStore.from_home(workspace),
                    runtime_root=_runtime_root(workspace),
                ),
            )
            for invalid_id in ("a:b", "a/b", "a\\b", "..", "含中文"):
                with self.subTest(server_id=invalid_id):
                    with self.assertRaises(ValueError):
                        await service.upsert_server(
                            server_id=invalid_id,
                            display_name="invalid",
                            transport="stdio",
                            transport_config={"command": "python"},
                            secrets={"TOKEN": "value"},
                        )

    async def test_existing_secret_namespace_is_restored_when_registry_write_fails(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                transport="stdio",
                transport_config={"command": "python"},
                secrets={"TOKEN": "old"},
            )

            with patch.object(
                registry,
                "upsert",
                AsyncMock(side_effect=OSError("disk full")),
            ):
                with self.assertRaises(OSError):
                    await service.upsert_server(
                        server_id="demo",
                        display_name="Demo",
                        transport="stdio",
                        transport_config={"command": "python"},
                        secrets={"TOKEN": "new"},
                    )

            self.assertEqual(
                secrets.snapshot_namespace("demo"),
                {"TOKEN": "old"},
            )


class McpAdapterTest(unittest.TestCase):
    def test_encode_tool_name_is_stable_and_namespaced(self):
        self.assertEqual(
            encode_tool_name("github", "search"),
            "mcp__github__search",
        )
        long_name = encode_tool_name("s", "x" * 80)
        self.assertLessEqual(len(long_name), 64)
        self.assertTrue(long_name.startswith("mcp__s__"))

    def test_adapt_call_result_preserves_error_content(self):
        result = CallToolResult(
            content=[TextContent(type="text", text="missing q")],
            isError=True,
        )
        adapted = adapt_call_result(result)
        self.assertFalse(adapted["ok"])
        self.assertEqual(adapted["code"], "MCP_TOOL_ERROR")
        self.assertEqual(adapted["data"]["mcp"]["content"][0]["text"], "missing q")

    def test_output_schema_requires_structured_content(self):
        validator = build_output_validator(
            "demo",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )
        result = CallToolResult(
            content=[TextContent(type="text", text="missing structured")],
            isError=False,
        )
        adapted = adapt_call_result(result, output_validator=validator)
        self.assertFalse(adapted["ok"])
        self.assertEqual(adapted["code"], "MCP_INVALID_TOOL_RESULT")

    def test_tool_error_does_not_need_to_match_output_schema(self):
        validator = build_output_validator(
            "demo",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )
        result = CallToolResult(
            content=[TextContent(type="text", text="business error")],
            isError=True,
        )
        adapted = adapt_call_result(result, output_validator=validator)
        self.assertEqual(adapted["code"], "MCP_TOOL_ERROR")

    def test_adapt_call_result_redacts_secrets_recursively(self):
        secret = "credential-that-must-not-leak"
        result = CallToolResult(
            content=[TextContent(type="text", text=f"token={secret}")],
            structuredContent={
                "token": secret,
                "nested": [f"Bearer {secret}"],
            },
            _meta={"debug": f"header={secret}"},
            isError=False,
        )

        adapted = adapt_call_result(result, secret_values=(secret,))

        serialized = json.dumps(adapted, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertIn("***", serialized)

    def test_redacted_result_cannot_leak_through_artifact_or_log(self):
        secret = "artifact-log-secret"
        result = CallToolResult(
            content=[TextContent(
                type="text",
                text=(f"credential={secret};" + "x" * 500),
            )],
            structuredContent={"credential": secret},
            isError=False,
        )
        adapted = adapt_call_result(result, secret_values=(secret,))

        with TemporaryDirectory() as directory:
            store = FileArtifactStore(Path(directory))
            body, artifact_id = externalize_payload(
                adapted,
                store,
                max_chars=500,
                preview_chars=80,
            )
            self.assertIsNotNone(artifact_id)
            artifact = store.read(artifact_id, 0, 10_000).content

        self.assertTrue(body["data"]["externalized"])
        self.assertNotIn(secret, artifact)
        self.assertNotIn(secret, json.dumps(body, ensure_ascii=False))


class McpProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_declaration_survives_registry_writes_and_filters_directory(self):
        await self.service.upsert_server(server_id="reader", display_name="Reader", transport="stdio",
                                         transport_config={"command": "python"}, read_only=True)
        await self.registry.set_enabled("reader", True)
        await self.registry.mark_tested("reader", RuntimeAvailability.AVAILABLE)
        await self.service.upsert_server(server_id="writer", display_name="Writer", transport="stdio",
                                         transport_config={"command": "python"}, enabled=True)
        descriptors = self.service.toolset_provider.descriptors(read_only_only=True)
        self.assertEqual([item.id for item in descriptors], ["mcp:reader"])
        record = await self.registry.get("reader")
        self.assertTrue(record.read_only)
        payload = record.to_dict()
        payload["read_only"] = 1
        with self.assertRaisesRegex(ValueError, "read_only"):
            McpServerRecord.from_dict(payload)

    async def test_connection_test_presents_tools_without_persisting_them(self):
        self.sessions["reader"] = FakeMcpSession(tools=[_tool("search")])
        await self.service.upsert_server(server_id="reader", display_name="Reader", transport="stdio",
                                         transport_config={"command": "python"}, read_only=True)
        result = await self.service.test_and_enable("reader")
        self.assertTrue(result.succeeded)
        self.assertEqual(result.runtime.tools[0]["name"], "search")
        self.assertEqual(self.manager.runtime_state("reader").tools, ())
        self.assertNotIn("tools", (await self.registry.get("reader")).to_dict())

    async def asyncSetUp(self):
        self._tmp = TemporaryDirectory()
        workspace = HelperMeHome(Path(self._tmp.name) / ".helperme")
        workspace.initialize()
        self.registry = McpRegistry.from_home(workspace)
        self.secrets = McpSecretStore.from_home(workspace)
        self.runtime_root = _runtime_root(workspace)
        self.sessions: dict[str, FakeMcpSession] = {}
        self.resolved_secrets: dict[str, dict[str, str]] = {}

        async def factory(record, secrets_map):
            session = self.sessions[record.id]
            self.resolved_secrets[record.id] = dict(secrets_map)
            if session.fail_initialize is not None:
                raise session.fail_initialize
            session.initialized = True
            stack = AsyncExitStack()
            await stack.__aenter__()
            return ManagedMcpConnection(
                session=session,
                stack=stack,
                record=record,
            )

        self.manager = McpClientManager(
            self.secrets,
            runtime_root=self.runtime_root,
            session_factory=factory,
        )
        self.service = McpApplicationService(
            self.registry,
            self.secrets,
            self.manager,
        )

    async def asyncTearDown(self):
        await self.manager.aclose()
        self._tmp.cleanup()

    async def test_descriptors_only_include_enabled_and_avoid_network(self):
        self.sessions["a"] = FakeMcpSession(tools=[_tool("search")])
        await self.service.upsert_server(
            server_id="a",
            display_name="A",
            description="alpha",
            transport="stdio",
            transport_config={"command": "python", "args": ["a.py"]},
            enabled=False,
        )
        await self.service.upsert_server(
            server_id="b",
            display_name="B",
            description="beta",
            transport="stdio",
            transport_config={"command": "python", "args": ["b.py"]},
            enabled=True,
        )
        descriptors = self.service.toolset_provider.descriptors()
        self.assertEqual([item.id for item in descriptors], ["mcp:b"])
        self.assertFalse(self.sessions["a"].initialized)

    async def test_load_toolset_discovers_and_routes_namespaced_tools(self):
        self.sessions["github"] = FakeMcpSession(tools=[_tool("search")])
        self.sessions["jira"] = FakeMcpSession(tools=[_tool("search")])
        for server_id in ("github", "jira"):
            await self.service.upsert_server(
                server_id=server_id,
                display_name=server_id,
                description=server_id,
                transport="stdio",
                transport_config={"command": "python", "args": [f"{server_id}.py"]},
                enabled=True,
            )

        github_specs = await self.service.toolset_provider.tool_specs("mcp:github")
        jira_specs = await self.service.toolset_provider.tool_specs("mcp:jira")
        self.assertEqual(github_specs[0].name, "mcp__github__search")
        self.assertEqual(jira_specs[0].name, "mcp__jira__search")
        self.assertIsInstance(github_specs[0].parameters, JsonSchemaParameters)

        result = await github_specs[0].handler({"q": "mcp"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.sessions["github"].calls, [("search", {"q": "mcp"})])
        self.assertEqual(self.sessions["jira"].calls, [])

    async def test_restore_tool_contract_connects_only_on_first_call(self):
        self.sessions["lazy"] = FakeMcpSession(tools=[_tool("search")])
        await self.service.upsert_server(
            server_id="lazy",
            display_name="Lazy",
            description="lazy restore",
            transport="stdio",
            transport_config={"command": "python", "args": ["lazy.py"]},
            enabled=True,
        )
        discovered = (
            await self.service.toolset_provider.discover_tools("mcp:lazy")
        )[0]
        await self.manager.invalidate("lazy")
        replacement = FakeMcpSession(tools=[_tool("search")])
        self.sessions["lazy"] = replacement

        spec = self.service.toolset_provider.restore_spec(
            toolset_id="mcp:lazy",
            revision=1,
            name=discovered.spec.name,
            description=discovered.spec.description,
            parameters=discovered.spec.parameters.schema(),
            requires_authorization=discovered.spec.requires_authorization,
            provider_data=discovered.provider_data,
        )

        self.assertFalse(replacement.initialized)
        result = await spec.handler({"q": "mcp"})
        self.assertTrue(replacement.initialized)
        self.assertTrue(result["ok"])

    async def test_connection_factory_receives_resolved_secret_values(self):
        self.sessions["secure"] = FakeMcpSession(tools=[_tool("search")])
        await self.service.upsert_server(
            server_id="secure",
            display_name="Secure",
            transport="stdio",
            transport_config={"command": "python"},
            secrets={"TOKEN": "secret-value"},
            enabled=True,
        )
        await self.service.toolset_provider.tool_specs("mcp:secure")
        self.assertEqual(
            self.resolved_secrets["secure"],
            {"TOKEN": "secret-value"},
        )

    async def test_load_failure_marks_runtime_unavailable_but_keeps_catalog(self):
        self.sessions["broken"] = FakeMcpSession(
            fail_initialize=ConnectionError("boom"),
        )
        await self.service.upsert_server(
            server_id="broken",
            display_name="Broken",
            description="broken",
            transport="stdio",
            transport_config={"command": "python", "args": ["x.py"]},
            enabled=True,
        )
        with self.assertRaises(ToolsetLoadError) as ctx:
            await self.service.toolset_provider.tool_specs("mcp:broken")
        self.assertEqual(ctx.exception.code, "MCP_TRANSPORT_ERROR")
        runtime = self.manager.runtime_state("broken")
        self.assertEqual(runtime.status, RuntimeAvailability.UNAVAILABLE)
        descriptors = self.service.toolset_provider.descriptors()
        self.assertEqual(descriptors[0].id, "mcp:broken")
        self.assertNotIn("最近失败", descriptors[0].description)

    async def test_revision_change_invalidates_client_cache(self):
        self.sessions["demo"] = FakeMcpSession(tools=[_tool("ping")])
        await self.service.upsert_server(
            server_id="demo",
            display_name="Demo",
            description="demo",
            transport="stdio",
            transport_config={"command": "python", "args": ["a.py"]},
            enabled=True,
        )
        old_specs = await self.service.toolset_provider.tool_specs("mcp:demo")
        first_session = self.sessions["demo"]
        self.assertTrue(first_session.initialized)

        replacement = FakeMcpSession(tools=[_tool("ping")])
        self.sessions["demo"] = replacement
        await self.service.upsert_server(
            server_id="demo",
            display_name="Demo2",
            description="demo2",
            transport="stdio",
            transport_config={"command": "python", "args": ["b.py"]},
            enabled=True,
        )
        await self.service.toolset_provider.tool_specs("mcp:demo")
        self.assertTrue(replacement.initialized)
        stale_result = await old_specs[0].handler({"q": "old snapshot"})
        self.assertFalse(stale_result["ok"])
        self.assertEqual(stale_result["code"], "MCP_SERVER_CHANGED")

    async def test_invalid_schema_fails_whole_toolset(self):
        self.sessions["bad"] = FakeMcpSession(
            tools=[
                _tool(
                    "broken",
                    schema={"type": "string"},
                )
            ]
        )
        await self.service.upsert_server(
            server_id="bad",
            display_name="Bad",
            description="bad",
            transport="stdio",
            transport_config={"command": "python", "args": ["bad.py"]},
            enabled=True,
        )
        with self.assertRaises(ToolsetLoadError) as ctx:
            await self.service.toolset_provider.tool_specs("mcp:bad")
        self.assertEqual(ctx.exception.code, "MCP_INVALID_TOOL_SCHEMA")

    async def test_invalid_output_schema_fails_whole_toolset(self):
        self.sessions["bad_output"] = FakeMcpSession(
            tools=[_tool("broken", output_schema={"type": "invalid"})]
        )
        await self.service.upsert_server(
            server_id="bad_output",
            display_name="Bad Output",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )
        with self.assertRaises(ToolsetLoadError) as ctx:
            await self.service.toolset_provider.tool_specs("mcp:bad_output")
        self.assertEqual(ctx.exception.code, "MCP_INVALID_TOOL_SCHEMA")

    async def test_content_service_uses_v2_resource_and_prompt_fields(self):
        session = FakeMcpSession()
        session.list_resources = AsyncMock(
            return_value=ListResourcesResult(
                resources=[
                    Resource(
                        name="guide",
                        uri="file:///guide.md",
                        mimeType="text/markdown",
                    )
                ],
                nextCursor="resource-next",
            )
        )
        session.list_resource_templates = AsyncMock(
            return_value=ListResourceTemplatesResult(
                resourceTemplates=[
                    ResourceTemplate(
                        name="user",
                        uriTemplate="user://{id}",
                        mimeType="application/json",
                    )
                ]
            )
        )
        session.list_prompts = AsyncMock(
            return_value=ListPromptsResult(
                prompts=[Prompt(name="review", description="review code")]
            )
        )
        self.sessions["content"] = session
        await self.service.upsert_server(
            server_id="content",
            display_name="Content",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )

        resources = await self.service.content.list_resources("content")
        templates = await self.service.content.list_resource_templates("content")
        prompts = await self.service.content.list_prompts("content")
        self.assertEqual(resources["next_cursor"], "resource-next")
        self.assertEqual(resources["resources"][0]["mimeType"], "text/markdown")
        self.assertEqual(
            templates["resource_templates"][0]["uriTemplate"],
            "user://{id}",
        )
        self.assertEqual(prompts["prompts"][0]["name"], "review")

    async def test_connection_is_closed_when_post_connect_metadata_fails(self):
        closed = False

        class BrokenMetadataSession(FakeMcpSession):
            @property
            def server_capabilities(self):
                raise RuntimeError("metadata broken")

        async def factory(record, secrets_map):
            nonlocal closed
            stack = AsyncExitStack()
            await stack.__aenter__()

            async def mark_closed():
                nonlocal closed
                closed = True

            stack.push_async_callback(mark_closed)
            return ManagedMcpConnection(
                session=BrokenMetadataSession(),
                stack=stack,
                record=record,
            )

        manager = McpClientManager(
            self.secrets,
            runtime_root=self.runtime_root,
            session_factory=factory,
        )
        service = McpApplicationService(self.registry, self.secrets, manager)
        await service.upsert_server(
            server_id="metadata",
            display_name="Metadata",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )
        with self.assertRaisesRegex(RuntimeError, "metadata broken"):
            await service.toolset_provider.tool_specs("mcp:metadata")
        self.assertTrue(closed)
        await manager.aclose()

    async def test_cancellation_waits_for_uncached_connection_cleanup(self):
        factory_entered = asyncio.Event()
        allow_factory_return = asyncio.Event()
        closed = asyncio.Event()

        async def factory(record, secrets_map):
            factory_entered.set()
            await allow_factory_return.wait()
            stack = AsyncExitStack()
            await stack.__aenter__()

            async def mark_closed():
                closed.set()

            stack.push_async_callback(mark_closed)
            return ManagedMcpConnection(
                session=FakeMcpSession(tools=[_tool("ping")]),
                stack=stack,
                record=record,
            )

        manager = McpClientManager(
            self.secrets,
            runtime_root=self.runtime_root,
            session_factory=factory,
        )
        service = McpApplicationService(self.registry, self.secrets, manager)
        await service.upsert_server(
            server_id="cancelled",
            display_name="Cancelled",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )
        load_task = asyncio.create_task(
            service.toolset_provider.tool_specs("mcp:cancelled")
        )
        await factory_entered.wait()
        await manager._lock.acquire()
        try:
            allow_factory_return.set()
            await asyncio.sleep(0)
            load_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await load_task
        finally:
            manager._lock.release()
        self.assertTrue(closed.is_set())
        await manager.aclose()

    async def test_disable_during_open_prevents_stale_connection_cache(self):
        factory_entered = asyncio.Event()
        allow_factory_return = asyncio.Event()
        closed = asyncio.Event()

        async def factory(record, secrets_map):
            factory_entered.set()
            await allow_factory_return.wait()
            stack = AsyncExitStack()
            await stack.__aenter__()

            async def mark_closed():
                closed.set()

            stack.push_async_callback(mark_closed)
            return ManagedMcpConnection(
                session=FakeMcpSession(tools=[_tool("ping")]),
                stack=stack,
                record=record,
            )

        manager = McpClientManager(
            self.secrets,
            runtime_root=self.runtime_root,
            session_factory=factory,
        )
        service = McpApplicationService(self.registry, self.secrets, manager)
        await service.upsert_server(
            server_id="disable_race",
            display_name="Disable race",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )
        load_task = asyncio.create_task(
            service.toolset_provider.tool_specs("mcp:disable_race")
        )
        await factory_entered.wait()
        await service.set_server_enabled("disable_race", False)
        allow_factory_return.set()
        with self.assertRaises(ToolsetLoadError):
            await load_task
        self.assertTrue(closed.is_set())
        self.assertNotIn("disable_race", manager._connections)
        await manager.aclose()

    async def test_build_mcp_wires_application_resource(self):
        mcp = build_mcp(
            HelperMeHome(Path(self._tmp.name) / ".helperme2"),
        )
        async with mcp.client_manager:
            self.assertIsNotNone(mcp.toolset_provider)


@pytest.mark.process
class McpRealStdioIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_reuses_state_across_toolset_loads(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            fixture = (
                Path(__file__).parents[1]
                / "fixtures"
                / "mcp_stdio_server.py"
            )
            await service.upsert_server(
                server_id="real_stdio",
                display_name="Real stdio",
                transport="stdio",
                transport_config={
                    "command": sys.executable,
                    "args": [str(fixture)],
                },
                secrets={"MCP_TEST_TOKEN": "stdio-secret"},
                enabled=True,
            )
            try:
                specs = await service.toolset_provider.tool_specs(
                    "mcp:real_stdio"
                )
                token_spec = next(
                    spec
                    for spec in specs
                    if spec.name.endswith("read_test_token")
                )
                result = await token_spec.handler({})
                self.assertTrue(result["ok"])
                self.assertEqual(
                    result["data"]["mcp"]["structured_content"]["token"],
                    "***",
                )
                counter_spec = next(
                    spec
                    for spec in specs
                    if spec.name.endswith("increment_counter")
                )
                first = await counter_spec.handler({})
                reloaded_specs = await service.toolset_provider.tool_specs(
                    "mcp:real_stdio"
                )
                reloaded_counter_spec = next(
                    spec
                    for spec in reloaded_specs
                    if spec.name.endswith("increment_counter")
                )
                second = await reloaded_counter_spec.handler({})
                self.assertEqual(
                    first["data"]["mcp"]["structured_content"]["count"],
                    1,
                )
                self.assertEqual(
                    second["data"]["mcp"]["structured_content"]["count"],
                    2,
                )
                cwd_spec = next(
                    spec
                    for spec in specs
                    if spec.name.endswith("read_working_directory")
                )
                cwd_result = await cwd_spec.handler({})
                expected_cwd = _runtime_root(workspace) / "real_stdio"
                self.assertEqual(
                    Path(
                        cwd_result["data"]["mcp"]["structured_content"]["cwd"]
                    ).resolve(),
                    expected_cwd.resolve(),
                )
                self.assertTrue(expected_cwd.is_dir())
                self.assertEqual(
                    manager.runtime_state("real_stdio").negotiated_version,
                    "2026-07-28",
                )
            finally:
                await manager.aclose()

    async def test_client_negotiates_with_legacy_server(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            fixture = (
                Path(__file__).parents[1]
                / "fixtures"
                / "mcp_legacy_stdio_server.py"
            )
            await service.upsert_server(
                server_id="legacy_stdio",
                display_name="Legacy stdio",
                transport="stdio",
                transport_config={
                    "command": sys.executable,
                    "args": [str(fixture)],
                },
                enabled=True,
            )
            try:
                specs = await service.toolset_provider.tool_specs(
                    "mcp:legacy_stdio"
                )
                self.assertEqual(specs, ())
                self.assertEqual(
                    manager.runtime_state("legacy_stdio").negotiated_version,
                    "2025-11-25",
                )
            finally:
                await manager.aclose()


class McpPaginationTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_list_collects_all_paginated_pages(self):
        pages = {
            None: ListToolsResult(
                tools=[_tool(f"tool_{index}") for index in range(50)],
                nextCursor="page-2",
                ttlMs=5_000,
            ),
            "page-2": ListToolsResult(
                tools=[_tool(f"tool_{index}") for index in range(50, 120)],
                ttlMs=3_000,
            ),
        }

        class PaginatedSession(FakeMcpSession):
            async def list_tools(self, *, cursor=None):
                return pages[cursor]

        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            manager = McpClientManager(
                McpSecretStore.from_home(workspace),
                runtime_root=_runtime_root(workspace),
            )
            tools, ttl = await manager._paginate_tools(PaginatedSession())

        self.assertEqual(len(tools), 120)
        self.assertEqual(tools[-1].name, "tool_119")
        self.assertEqual(ttl, 3.0)


@pytest.mark.process
class McpRealStreamableHttpIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    async def test_real_streamable_http_lists_and_calls_tool(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            fixture = (
                Path(__file__).parents[1]
                / "fixtures"
                / "mcp_streamable_http_server.py"
            )
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(fixture),
                "--port",
                str(port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await service.upsert_server(
                server_id="real_http",
                display_name="Real HTTP",
                transport="streamable_http",
                transport_config={
                    "url": f"http://127.0.0.1:{port}/mcp",
                    "timeout_seconds": 2,
                },
                enabled=True,
            )
            try:
                specs = None
                for _ in range(30):
                    if process.returncode is not None:
                        stderr = (await process.stderr.read()).decode(
                            errors="replace"
                        )
                        self.fail(f"HTTP fixture exited early: {stderr}")
                    try:
                        specs = await service.toolset_provider.tool_specs(
                            "mcp:real_http"
                        )
                        break
                    except ToolsetLoadError:
                        await asyncio.sleep(0.1)
                self.assertIsNotNone(specs)
                echo = next(spec for spec in specs if spec.name.endswith("echo"))
                result = await echo.handler({"value": "hello-http"})
                self.assertTrue(result["ok"])
                self.assertEqual(
                    result["data"]["mcp"]["structured_content"]["value"],
                    "hello-http",
                )
            finally:
                await manager.aclose()
                if process.returncode is None:
                    process.terminate()
                await process.wait()


@pytest.mark.process
class McpRealStdioWorkingDirectoryIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    async def test_stdio_preserves_explicit_working_directory(self):
        with TemporaryDirectory() as directory:
            workspace = HelperMeHome(Path(directory) / ".helperme")
            workspace.initialize()
            explicit_cwd = Path(directory) / "explicit-mcp-cwd"
            explicit_cwd.mkdir()
            registry = McpRegistry.from_home(workspace)
            secrets = McpSecretStore.from_home(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            fixture = (
                Path(__file__).parents[1]
                / "fixtures"
                / "mcp_stdio_server.py"
            )
            await service.upsert_server(
                server_id="explicit_stdio",
                display_name="Explicit stdio",
                transport="stdio",
                transport_config={
                    "command": sys.executable,
                    "args": [str(fixture)],
                    "cwd": str(explicit_cwd),
                },
                enabled=True,
            )
            try:
                specs = await service.toolset_provider.tool_specs(
                    "mcp:explicit_stdio"
                )
                cwd_spec = next(
                    spec
                    for spec in specs
                    if spec.name.endswith("read_working_directory")
                )
                result = await cwd_spec.handler({})
                self.assertEqual(
                    Path(
                        result["data"]["mcp"]["structured_content"]["cwd"]
                    ).resolve(),
                    explicit_cwd.resolve(),
                )
            finally:
                await manager.aclose()
