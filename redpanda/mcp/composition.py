from __future__ import annotations

from dataclasses import dataclass

from redpanda.paths import RedPandaHome
from redpanda.mcp.application import McpApplicationService
from redpanda.mcp.client_manager import McpClientManager
from redpanda.mcp.content import McpContentService
from redpanda.mcp.registry import McpRegistry
from redpanda.mcp.secrets import McpSecretStore
from redpanda.mcp.approval import (
    McpInstallApprovalHandler,
    McpRecoveryApprovalHandler,
    McpRemoveApprovalHandler,
    McpUpdateApprovalHandler,
    create_mcp_install_proposal_spec,
    create_mcp_recovery_proposal_spec,
    create_mcp_remove_proposal_spec,
    create_mcp_update_proposal_spec,
)
from redpanda.mcp.management_tools import create_mcp_management_specs
from redpanda.tools.control import ControlOperation
from redpanda.tools.spec import ToolSpec


@dataclass
class McpAssembly:
    service: McpApplicationService
    client_manager: McpClientManager
    management_specs: tuple[ToolSpec, ...]
    control_operations: tuple[ControlOperation, ...]

    @property
    def toolset_provider(self):
        return self.service.toolset_provider


def build_mcp(
    home: RedPandaHome,
    *,
    client_manager: McpClientManager | None = None,
) -> McpAssembly:
    registry = McpRegistry.from_home(home)
    secret_store = McpSecretStore.from_home(home)
    manager = (
        McpClientManager(
            secret_store,
            runtime_root=home.mcp_root / "runtime",
        )
        if client_manager is None
        else client_manager
    )
    content = McpContentService(registry, manager)
    service = McpApplicationService(
        registry,
        secret_store,
        manager,
        content_service=content,
    )
    control_operations = (
        ControlOperation(
            "mcp",
            create_mcp_install_proposal_spec(service),
            McpInstallApprovalHandler(service),
        ),
        ControlOperation(
            "mcp",
            create_mcp_recovery_proposal_spec(service),
            McpRecoveryApprovalHandler(service),
        ),
        ControlOperation(
            "mcp",
            create_mcp_update_proposal_spec(service),
            McpUpdateApprovalHandler(service),
        ),
        ControlOperation(
            "mcp",
            create_mcp_remove_proposal_spec(service),
            McpRemoveApprovalHandler(service),
        ),
    )
    return McpAssembly(
        service=service,
        client_manager=manager,
        management_specs=create_mcp_management_specs(service),
        control_operations=control_operations,
    )
