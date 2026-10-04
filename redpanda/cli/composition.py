from __future__ import annotations

import os
from dataclasses import dataclass

from redpanda.cli.approval import (
    CliInstallApprovalHandler,
    CliRepairApprovalHandler,
    CliUninstallApprovalHandler,
    CliUpdateApprovalHandler,
    create_cli_install_proposal_spec,
    create_cli_repair_proposal_spec,
    create_cli_uninstall_proposal_spec,
    create_cli_update_proposal_spec,
)
from redpanda.cli.application import CliApplicationService
from redpanda.cli.management_tools import create_cli_management_specs
from redpanda.cli.runtime import CliToolCatalog
from redpanda.paths import RedPandaHome
from redpanda.sandbox.command import EnvironmentCommandExecutor
from redpanda.tools.control import ControlOperation
from redpanda.tools.spec import ToolSpec


@dataclass(frozen=True)
class CliAssembly:
    service: CliApplicationService
    management_specs: tuple[ToolSpec, ...]
    control_operations: tuple[ControlOperation, ...]

    @property
    def tool_catalog(self) -> CliToolCatalog:
        return self.service.tool_catalog


def build_cli(
    home: RedPandaHome,
    command_executor: EnvironmentCommandExecutor | None = None,
) -> CliAssembly:
    """装配 CLI 域。

    command_executor 是进程级单例：包管理器操作与体检和 workspace 无关，
    不经过 per-Session 的 EnvironmentBinding。
    """
    if command_executor is None:
        command_executor = _default_command_executor()
    service = CliApplicationService(home, command_executor)
    control_operations = (
        ControlOperation(
            "cli",
            create_cli_install_proposal_spec(service),
            CliInstallApprovalHandler(service),
        ),
        ControlOperation(
            "cli",
            create_cli_uninstall_proposal_spec(service),
            CliUninstallApprovalHandler(service),
        ),
        ControlOperation(
            "cli",
            create_cli_update_proposal_spec(service),
            CliUpdateApprovalHandler(service),
        ),
        ControlOperation(
            "cli",
            create_cli_repair_proposal_spec(service),
            CliRepairApprovalHandler(service),
        ),
    )
    return CliAssembly(
        service=service,
        management_specs=create_cli_management_specs(service),
        control_operations=control_operations,
    )


def _default_command_executor() -> EnvironmentCommandExecutor:
    if os.name == "nt":
        from redpanda.sandbox.local.powershell import PowerShellCommandRunner

        return PowerShellCommandRunner()
    from redpanda.sandbox.local.bash import BashCommandRunner

    return BashCommandRunner()
