from __future__ import annotations

from typing import Mapping, cast

from pydantic import BaseModel

from redpanda.cli.application import CliApplicationService
from redpanda.cli.errors import (
    CliAlreadyInstalledError,
    CliInputError,
    CliNotFoundError,
    CliSourceError,
)
from redpanda.cli.models import CliHealth, CliSourceRef
from redpanda.tools.control import (
    ControlApprovalExecution,
    ControlApprovalProposal,
    ControlPreparationFailure,
)
from redpanda.tools.spec import PydanticParameters, ToolSpec


CLI_INSTALL_ACTION = "cli.install"
CLI_UNINSTALL_ACTION = "cli.uninstall"
CLI_UPDATE_ACTION = "cli.update"
CLI_REPAIR_ACTION = "cli.repair"

PROPOSE_CLI_INSTALL = "propose_cli_install"
PROPOSE_CLI_UNINSTALL = "propose_cli_uninstall"
PROPOSE_CLI_UPDATE = "propose_cli_update"
PROPOSE_CLI_REPAIR = "propose_cli_repair"


class CliInstallProposalInput(BaseModel):
    name: str
    description: str
    locator: str


def _health_summary(record_health: CliHealth) -> str:
    return (
        f"  --help：{'正常' if record_health.help_ok else '失败'}\n"
        f"  --version：{'正常' if record_health.version_ok else '失败'}\n"
        f"  提及 JSON 输出：{'是' if record_health.help_mentions_json else '否'}"
    )


def create_cli_install_proposal_spec(
    service: CliApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: CliInstallProposalInput,
    ) -> ControlApprovalProposal | ControlPreparationFailure | dict:
        try:
            candidate = await service.prepare_install(
                input_data.name,
                input_data.description,
                input_data.locator,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "code": "CLI_INVALID",
                "data": {"name": input_data.name},
                "error": str(exc),
                "hint": "name 需匹配 ^[a-z0-9][a-z0-9-]{0,63}$；description 为单行。",
            }
        except CliAlreadyInstalledError as exc:
            return {
                "ok": False,
                "code": "CLI_ALREADY_INSTALLED",
                "data": {"name": input_data.name},
                "error": str(exc),
                "hint": "检查已登记目录；版本变化应走 propose_cli_update。",
            }
        except (CliInputError, CliSourceError) as exc:
            return ControlPreparationFailure({
                "ok": False,
                "code": "CLI_SOURCE_ERROR",
                "data": {"locator": input_data.locator},
                "error": str(exc),
                "hint": "确认该 CLI 已安装且在 PATH 中，或提供显式可执行文件路径。",
            })
        return ControlApprovalProposal(
            action=CLI_INSTALL_ACTION,
            payload={
                "name": candidate.name,
                "description": candidate.description,
                "source": candidate.source.to_dict(),
                "resolved_path": candidate.resolved_path,
            },
            summary=(
                f"准备登记 CLI `{candidate.name}`\n"
                f"描述：{candidate.description}\n"
                f"来源：{candidate.source.kind} {candidate.source.locator}\n"
                f"解析路径：{candidate.resolved_path}\n"
                f"版本：{candidate.probed.version or '未知'}\n"
                f"体检：\n{_health_summary(candidate.probed.health)}"
            ),
            risk=(
                "登记后该 CLI 立即出现在能力目录中可用（没有启用/停用开关）；"
                "其本地凭据（如 gh 的 GitHub 凭据）将对助手开放。"
                "只登记本机已安装的程序，不下载任何软件。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_CLI_INSTALL,
        description=(
            "当用户要求登记一个本机已安装的 CLI 时，解析路径并预跑体检，"
            "然后提交用户审批。信息不足时先询问；本工具必须单独调用。"
        ),
        parameters=PydanticParameters(CliInstallProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class CliInstallApprovalHandler:
    action = CLI_INSTALL_ACTION

    def __init__(self, service: CliApplicationService) -> None:
        self.service = service

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        if set(payload) != {"name", "description", "source", "resolved_path"}:
            raise CliInputError("CLI install approval payload 字段不匹配")
        source = payload["source"]
        if not isinstance(source, Mapping):
            raise CliInputError("CLI install approval source 必须是 object")
        name = payload["name"]
        description = payload["description"]
        resolved_path = payload["resolved_path"]
        if any(
            type(value) is not str
            for value in (name, description, resolved_path)
        ):
            raise CliInputError("CLI install approval identity 类型无效")
        try:
            record = await self.service.install_frozen(
                cast(str, name),
                cast(str, description),
                CliSourceRef.from_dict(dict(source)),
                cast(str, resolved_path),
            )
        except (CliAlreadyInstalledError, CliInputError) as exc:
            return ControlApprovalExecution(
                False,
                f"CLI `{name}` 登记未执行：{exc}",
            )
        return ControlApprovalExecution(
            True,
            f"CLI `{record.name}` 已登记。目录将从下一次决策生效。",
            data={
                "cli_id": record.name,
                "revision": record.revision,
                "version": record.version,
            },
        )


class CliIdProposalInput(BaseModel):
    cli_id: str


def create_cli_uninstall_proposal_spec(
    service: CliApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: CliIdProposalInput,
    ) -> ControlApprovalProposal | dict:
        record = await service.inspect_or_none(input_data.cli_id)
        if record is None:
            return _not_installed(input_data.cli_id)
        return ControlApprovalProposal(
            action=CLI_UNINSTALL_ACTION,
            payload={
                "cli_id": record.name,
                "expected_revision": record.revision,
            },
            summary=(
                f"准备移除 CLI `{record.name}` 的登记\n"
                f"Revision：{record.revision}\n"
                f"解析路径：{record.resolved_path}"
            ),
            risk=(
                "只移除登记，不卸载软件本身；"
                "移除后该 CLI 不再出现在能力目录中。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_CLI_UNINSTALL,
        description=(
            "移除一个已登记 CLI 的登记事实（不卸载软件）。本工具必须单独调用。"
        ),
        parameters=PydanticParameters(CliIdProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


def create_cli_update_proposal_spec(
    service: CliApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: CliIdProposalInput,
    ) -> ControlApprovalProposal | dict:
        record = await service.inspect_or_none(input_data.cli_id)
        if record is None:
            return _not_installed(input_data.cli_id)
        return ControlApprovalProposal(
            action=CLI_UPDATE_ACTION,
            payload={
                "cli_id": record.name,
                "expected_revision": record.revision,
            },
            summary=(
                f"准备刷新 CLI `{record.name}` 的版本与体检信息（不安装新版本）\n"
                f"Revision：{record.revision}\n"
                f"当前版本：{record.version or '未知'}\n"
                "将按登记路径重跑体检，更新 version 与 health。"
            ),
            risk=(
                "refresh 不重解析路径；登记路径已失效时应改用 "
                "propose_cli_repair。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_CLI_UPDATE,
        description=(
            "用户自行升级某 CLI 后，重跑体检刷新登记的 version/health。"
            "登记路径失效时不要调用，应改用 propose_cli_repair。"
            "本工具必须单独调用。"
        ),
        parameters=PydanticParameters(CliIdProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


def create_cli_repair_proposal_spec(
    service: CliApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: CliIdProposalInput,
    ) -> ControlApprovalProposal | dict:
        record = await service.inspect_or_none(input_data.cli_id)
        if record is None:
            return _not_installed(input_data.cli_id)
        return ControlApprovalProposal(
            action=CLI_REPAIR_ACTION,
            payload={
                "cli_id": record.name,
                "expected_revision": record.revision,
            },
            summary=(
                f"准备修复 CLI `{record.name}`\n"
                f"Revision：{record.revision}\n"
                f"登记路径：{record.resolved_path}\n"
                "将按登记来源重新解析 resolved_path 并重跑体检。"
            ),
            risk="repair 只重新解析与体检，不会暗中变更登记身份。",
        )

    return ToolSpec(
        name=PROPOSE_CLI_REPAIR,
        description=(
            "在 inspect/test 已证明登记路径失效后，按登记来源重新解析 "
            "resolved_path 并提交修复审批。本工具必须单独调用。"
        ),
        parameters=PydanticParameters(CliIdProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class _CliRevisionApprovalHandler:
    """uninstall / update / repair 共用的 revision 乐观锁执行骨架。"""

    def __init__(self, service: CliApplicationService) -> None:
        self.service = service

    async def _execute_at_revision(
        self,
        payload: Mapping[str, object],
        operation,
        verb: str,
    ) -> ControlApprovalExecution:
        if set(payload) != {"cli_id", "expected_revision"}:
            raise CliInputError(f"CLI {verb} approval payload 字段不匹配")
        cli_id = payload["cli_id"]
        expected_revision = payload["expected_revision"]
        if type(cli_id) is not str or type(expected_revision) is not int:
            raise CliInputError(f"CLI {verb} approval payload 类型无效")
        try:
            record = await operation(
                cli_id,
                expected_revision=expected_revision,
            )
        except (CliNotFoundError, CliInputError, CliSourceError) as exc:
            return ControlApprovalExecution(
                False,
                f"CLI `{cli_id}` {verb}未执行：{exc}",
            )
        return ControlApprovalExecution(
            True,
            f"CLI `{record.name}` 已{verb} (revision={record.revision})。",
            {"cli_id": record.name, "revision": record.revision},
        )


class CliUninstallApprovalHandler(_CliRevisionApprovalHandler):
    action = CLI_UNINSTALL_ACTION

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        return await self._execute_at_revision(
            payload,
            self.service.uninstall,
            "移除登记",
        )


class CliUpdateApprovalHandler(_CliRevisionApprovalHandler):
    action = CLI_UPDATE_ACTION

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        return await self._execute_at_revision(
            payload,
            self.service.refresh,
            "刷新",
        )


class CliRepairApprovalHandler(_CliRevisionApprovalHandler):
    action = CLI_REPAIR_ACTION

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        return await self._execute_at_revision(
            payload,
            self.service.repair,
            "修复",
        )


def _not_installed(cli_id: str) -> dict:
    return {
        "ok": False,
        "code": "CLI_NOT_INSTALLED",
        "data": {"cli_id": cli_id},
        "error": f"CLI 未登记: {cli_id}",
        "hint": "先调用 list_installed_clis 查看管理目录。",
    }
