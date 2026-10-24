import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from konden.mcp.application import McpApplicationService
from konden.mcp.client_manager import McpClientManager
from konden.mcp.registry import McpRegistry
from konden.mcp.secrets import McpSecretStore
from konden.paths import KondenHome
from konden.tools.control import ControlApprovalProposal
from konden.tools.spec import ToolArgumentsError
from konden.mcp.approval import (
    MCP_INSTALL_ACTION,
    MCP_RECOVER_ACTION,
    MCP_REMOVE_ACTION,
    McpInstallApprovalHandler,
    McpRecoveryApprovalHandler,
    McpRemoveApprovalHandler,
    McpRemoveProposalInput,
    create_mcp_install_proposal_spec,
    create_mcp_recovery_proposal_spec,
    create_mcp_remove_proposal_spec,
    create_mcp_update_proposal_spec,
)
from konden.mcp.errors import McpRecoveryPreconditionError
from konden.mcp.models import McpServerRuntimeState, RuntimeAvailability
from konden.mcp.console import McpConsoleAdapter


class McpInstallProposalTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _spec(existing=None):
        service = SimpleNamespace(
            registry=SimpleNamespace(get=AsyncMock(return_value=existing)),
        )
        return create_mcp_install_proposal_spec(service)

    async def test_agent_constructs_frozen_stdio_proposal(self):
        spec = self._spec()
        input_data = spec.parameters.validate({
            "server_id": "filesystem",
            "display_name": "Filesystem MCP",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "E:\\myCard"],
            "source": "official_documentation",
        })

        result = await spec.handler(input_data)

        self.assertIsInstance(result, ControlApprovalProposal)
        self.assertEqual(result.action, MCP_INSTALL_ACTION)
        self.assertTrue(spec.control_boundary)
        self.assertNotIn("secrets", result.payload)
        self.assertIn("Executable：npx", result.summary)

    async def test_yes_install_accepts_frozen_stdio_args(self):
        spec = self._spec()
        input_data = spec.parameters.validate({
            "server_id": "playwright",
            "display_name": "Playwright",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
            "source": "official_documentation",
        })
        request = await spec.handler(input_data)

        with TemporaryDirectory() as directory:
            home = KondenHome(Path(directory) / ".konden")
            home.initialize()
            secrets = McpSecretStore.from_home(home)
            service = McpApplicationService(
                McpRegistry.from_home(home),
                secrets,
                McpClientManager(secrets, runtime_root=home.mcp_root / "runtime"),
            )
            record = await service.upsert_server(
                server_id=request.payload["server_id"],
                display_name=request.payload["display_name"],
                description=request.payload["description"],
                transport=request.payload["transport"],
                transport_config=request.payload["transport_config"],
                enabled=False,
            )

        self.assertEqual(record.transport_config.args, ("-y", "@playwright/mcp@latest"))

    async def test_secrets_pass_through_payload_but_not_summary(self):
        spec = self._spec()
        cases = (
            ({
                "transport": "streamable_http",
                "url": "https://example.com/mcp",
                "headers": {"X-API-Key": "secret-value"},
                "bearer": "token-value",
            }, '"X-API-Key": "***"'),
            ({
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "tavily-mcp"],
                "env": {"TAVILY_API_KEY": "secret-value"},
            }, '"TAVILY_API_KEY": "***"'),
            ({
                "transport": "streamable_http",
                "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=secret-value",
            }, "?<redacted>"),
        )
        for fields, marker in cases:
            with self.subTest(marker=marker):
                result = await spec.handler(spec.parameters.validate({
                    "server_id": "remote",
                    "display_name": "Remote",
                    "source": "user_input",
                    **fields,
                }))

                self.assertIsInstance(result, ControlApprovalProposal)
                config = result.payload["transport_config"]
                for key in ("url", "headers", "bearer", "env"):
                    if key in fields:
                        self.assertEqual(config[key], fields[key])
                self.assertNotIn("secret-value", result.summary)
                self.assertNotIn("token-value", result.summary)
                self.assertIn(marker, result.summary)

    async def test_rejects_invalid_user_or_model_input(self):
        spec = self._spec()
        stdio = {"transport": "stdio", "command": "npx", "args": ["server"]}
        http = {"transport": "streamable_http", "url": "https://example.com/mcp"}
        cases = (
            {**stdio, "command": "cmd.exe", "args": ["/c", "npm install x && x"]},
            {**stdio, "source": "model_inference"},
            {**http, "url": "https://user:pass@example.com/mcp"},
            {**stdio, "headers": {"Authorization": "secret"}},
            {**stdio, "bearer": "secret"},
            {**http, "env": {"KEY": "secret"}},
        )
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ToolArgumentsError):
                spec.parameters.validate({
                    "server_id": "demo",
                    "display_name": "Demo",
                    "source": "user_input",
                    **fields,
                })

    async def test_existing_id_is_not_silently_overwritten(self):
        existing = SimpleNamespace(id="demo", enabled=True, revision=7)
        spec = self._spec(existing)
        input_data = spec.parameters.validate({
            "server_id": "demo",
            "display_name": "Demo",
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
            "source": "user_input",
        })

        result = await spec.handler(input_data)

        self.assertEqual(result["code"], "MCP_SERVER_ALREADY_REGISTERED")

    async def test_update_is_explicit_and_freezes_existing_revision(self):
        existing = SimpleNamespace(id="demo", enabled=True, revision=7)
        service = SimpleNamespace(
            registry=SimpleNamespace(get=AsyncMock(return_value=existing)),
        )
        spec = create_mcp_update_proposal_spec(service)
        input_data = spec.parameters.validate({
            "server_id": "demo",
            "display_name": "Demo v2",
            "transport": "stdio",
            "command": "python",
            "args": ["server-v2.py"],
            "source": "official_documentation",
        })

        request = await spec.handler(input_data)

        self.assertIsInstance(request, ControlApprovalProposal)
        self.assertEqual(request.payload["expected_revision"], 7)
        self.assertEqual(
            request.payload["transport_config"]["args"],
            ("server-v2.py",),
        )


class McpInstallApprovalHandlerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SimpleNamespace(
            upsert_server=AsyncMock(),
            test_and_enable=AsyncMock(),
        )
        self.handler = McpInstallApprovalHandler(self.service)
        self.payload = {
            "server_id": "demo",
            "display_name": "Demo",
            "description": "demo server",
            "read_only": False,
            "transport": "stdio",
            "transport_config": {
                "command": "python",
                "args": ["server.py"],
                "cwd": None,
            },
            "source": "user_input",
        }

    async def test_registers_disabled_then_tests_then_enables(self):
        self.service.upsert_server.return_value = SimpleNamespace(
            id="demo",
            revision=1,
        )
        runtime = McpServerRuntimeState(status=RuntimeAvailability.AVAILABLE)
        enabled = SimpleNamespace(
            id="demo",
            revision=2,
            toolset_id="mcp:demo",
        )
        self.service.test_and_enable.return_value = SimpleNamespace(
            succeeded=True,
            runtime=runtime,
            record=enabled,
        )

        result = await self.handler.execute(self.payload)

        self.assertTrue(result.succeeded)
        self.assertIs(self.service.upsert_server.await_args.kwargs["enabled"], False)
        self.service.test_and_enable.assert_awaited_once_with(
            "demo",
            expected_revision=1,
        )

    async def test_failed_test_keeps_server_disabled(self):
        self.service.upsert_server.return_value = SimpleNamespace(
            id="demo",
            revision=1,
        )
        runtime = McpServerRuntimeState(
            status=RuntimeAvailability.UNAVAILABLE,
            last_error_summary="connection failed",
        )
        self.service.test_and_enable.return_value = SimpleNamespace(
            succeeded=False,
            runtime=runtime,
            record=SimpleNamespace(id="demo", revision=1),
        )

        result = await self.handler.execute(self.payload)

        self.assertFalse(result.succeeded)
        self.assertFalse(result.data["enabled"])


class McpRecoveryApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def test_proposal_freezes_disabled_server_revision(self):
        record = SimpleNamespace(
            id="demo",
            display_name="Demo",
            enabled=False,
            revision=4,
        )
        service = SimpleNamespace(
            registry=SimpleNamespace(get=AsyncMock(return_value=record)),
        )
        spec = create_mcp_recovery_proposal_spec(service)
        input_data = spec.parameters.validate({"server_id": "demo"})

        result = await spec.handler(input_data)

        self.assertIsInstance(result, ControlApprovalProposal)
        self.assertEqual(result.action, MCP_RECOVER_ACTION)
        self.assertEqual(result.payload["expected_revision"], 4)
        self.assertTrue(spec.control_boundary)

    async def test_missing_server_does_not_create_approval(self):
        service = SimpleNamespace(
            registry=SimpleNamespace(get=AsyncMock(return_value=None)),
        )
        spec = create_mcp_recovery_proposal_spec(service)
        input_data = spec.parameters.validate({"server_id": "missing"})

        result = await spec.handler(input_data)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "MCP_SERVER_NOT_FOUND")

    async def test_handler_tests_and_enables_expected_revision(self):
        runtime = McpServerRuntimeState(status=RuntimeAvailability.AVAILABLE)
        enabled = SimpleNamespace(id="demo", revision=5, toolset_id="mcp:demo")
        service = SimpleNamespace(
            test_and_enable=AsyncMock(return_value=SimpleNamespace(
                succeeded=True,
                runtime=runtime,
                record=enabled,
            )),
        )
        handler = McpRecoveryApprovalHandler(service)

        result = await handler.execute({
            "server_id": "demo",
            "expected_revision": 4,
        })

        self.assertTrue(result.succeeded)
        service.test_and_enable.assert_awaited_once_with(
            "demo",
            expected_revision=4,
        )
        self.assertTrue(result.data["enabled"])

    async def test_handler_only_converts_declared_recovery_precondition(self):
        service = SimpleNamespace(
            test_and_enable=AsyncMock(
                side_effect=McpRecoveryPreconditionError("revision changed")
            ),
        )
        handler = McpRecoveryApprovalHandler(service)

        result = await handler.execute({
            "server_id": "demo",
            "expected_revision": 4,
        })

        self.assertFalse(result.succeeded)
        self.assertIn("revision changed", result.message)

    async def test_handler_exposes_unexpected_service_value_error(self):
        service = SimpleNamespace(
            test_and_enable=AsyncMock(
                side_effect=ValueError("internal service bug")
            ),
        )
        handler = McpRecoveryApprovalHandler(service)

        with self.assertRaisesRegex(ValueError, "internal service bug"):
            await handler.execute({
                "server_id": "demo",
                "expected_revision": 4,
            })

    async def test_handler_keeps_unavailable_server_disabled(self):
        runtime = McpServerRuntimeState(
            status=RuntimeAvailability.UNAVAILABLE,
            last_error_summary="connection failed",
        )
        service = SimpleNamespace(
            test_and_enable=AsyncMock(return_value=SimpleNamespace(
                succeeded=False,
                runtime=runtime,
                record=SimpleNamespace(id="demo", revision=4),
            )),
        )
        handler = McpRecoveryApprovalHandler(service)

        result = await handler.execute({
            "server_id": "demo",
            "expected_revision": 4,
        })

        self.assertFalse(result.succeeded)
        self.assertFalse(result.data["enabled"])
        self.assertEqual(
            result.data["runtime"]["last_error_summary"],
            "connection failed",
        )


class McpRecoveryConsoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_retry_reuses_atomic_test_and_enable_use_case(self):
        runtime = McpServerRuntimeState(status=RuntimeAvailability.AVAILABLE)
        service = SimpleNamespace(
            test_and_enable=AsyncMock(return_value=SimpleNamespace(
                succeeded=True,
                runtime=runtime,
                record=SimpleNamespace(id="demo", revision=5),
            )),
        )
        console = McpConsoleAdapter(service)

        reply = await console.execute_if_handled("/mcp retry demo")

        self.assertIn("测试并启用成功", reply)
        self.assertIn("能力目录", reply)


class McpRemoveApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def test_proposal_freezes_registered_revision(self):
        record = SimpleNamespace(
            id="demo",
            display_name="Demo",
            enabled=True,
            revision=3,
        )
        service = SimpleNamespace(
            registry=SimpleNamespace(get=AsyncMock(return_value=record)),
        )
        spec = create_mcp_remove_proposal_spec(service)
        input_data = spec.parameters.validate({"server_id": "demo"})

        result = await spec.handler(input_data)

        self.assertIsInstance(result, ControlApprovalProposal)
        self.assertEqual(result.action, MCP_REMOVE_ACTION)
        self.assertEqual(result.payload["expected_revision"], 3)
        self.assertTrue(spec.control_boundary)

    async def test_missing_server_does_not_create_approval(self):
        service = SimpleNamespace(
            registry=SimpleNamespace(get=AsyncMock(return_value=None)),
        )
        spec = create_mcp_remove_proposal_spec(service)
        result = await spec.handler(McpRemoveProposalInput(server_id="missing"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "MCP_SERVER_NOT_FOUND")

    async def test_handler_removes_expected_revision(self):
        service = SimpleNamespace(
            remove_server=AsyncMock(
                return_value=SimpleNamespace(id="demo", revision=3)
            ),
        )
        handler = McpRemoveApprovalHandler(service)

        result = await handler.execute({
            "server_id": "demo",
            "expected_revision": 3,
        })

        self.assertTrue(result.succeeded)
        service.remove_server.assert_awaited_once_with(
            "demo",
            expected_revision=3,
        )

    async def test_handler_only_converts_declared_precondition(self):
        service = SimpleNamespace(
            remove_server=AsyncMock(
                side_effect=McpRecoveryPreconditionError("revision changed")
            ),
        )
        handler = McpRemoveApprovalHandler(service)

        result = await handler.execute({
            "server_id": "demo",
            "expected_revision": 3,
        })

        self.assertFalse(result.succeeded)
        self.assertIn("revision changed", result.message)
