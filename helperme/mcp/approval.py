from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helperme.tools.control import ControlApprovalExecution, ControlApprovalProposal
from helperme.tools.spec import PydanticParameters, ToolSpec
from helperme.mcp.application import McpApplicationService
from helperme.mcp.errors import McpInputError, McpRecoveryPreconditionError


MCP_INSTALL_ACTION = "mcp.install"
MCP_RECOVER_ACTION = "mcp.recover"
MCP_UPDATE_ACTION = "mcp.update"
MCP_REMOVE_ACTION = "mcp.remove"
PROPOSE_MCP_INSTALL = "propose_mcp_install"
PROPOSE_MCP_RECOVERY = "propose_mcp_recovery"
PROPOSE_MCP_UPDATE = "propose_mcp_update"
PROPOSE_MCP_REMOVE = "propose_mcp_remove"

_SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "sh",
    "zsh",
}


class McpInstallProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str
    display_name: str
    description: str = ""
    read_only: bool = Field(default=False, description="声明此安装配置只提供只读能力；由配置者保证 transport 的限制。")
    transport: Literal["stdio", "streamable_http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    bearer: str | None = None
    timeout_seconds: float = 30.0
    source: Literal["user_input", "official_documentation", "registry"]

    @model_validator(mode="after")
    def validate_transport(self) -> "McpInstallProposalInput":
        if self.transport == "stdio":
            if self.command is None or not self.command.strip():
                raise ValueError("stdio proposal 必须提供 command")
            if self.url is not None:
                raise ValueError("stdio proposal 不能提供 url")
            if self.headers or self.bearer is not None:
                raise ValueError(
                    "stdio 的密钥请填 env，不要填 headers/bearer"
                )
            executable = PurePath(self.command).name.lower()
            if executable in _SHELL_EXECUTABLES:
                raise ValueError("MCP stdio 不接受 Shell 解释器作为 command")
            if "\n" in self.command or "\r" in self.command:
                raise ValueError("stdio command 不能包含换行")
            if any("\n" in arg or "\r" in arg for arg in self.args):
                raise ValueError("stdio args 不能包含换行")
        else:
            if self.url is None or not self.url.strip():
                raise ValueError("streamable_http proposal 必须提供 url")
            if self.command is not None or self.args or self.cwd is not None:
                raise ValueError(
                    "streamable_http proposal 不能提供 command/args/cwd"
                )
            if self.env:
                raise ValueError(
                    "streamable_http 的鉴权请填 headers 或 bearer，不要填 env"
                )
            parsed = urlsplit(self.url)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(
                    "streamable_http URL 不能包含 userinfo 凭据"
                )
        return self

    def transport_config(self) -> dict[str, Any]:
        if self.transport == "stdio":
            config = {
                "command": self.command,
                "args": list(self.args),
                "cwd": self.cwd,
            }
            if self.env:
                config["env"] = self.env
            return config
        config = {
            "url": self.url,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.headers:
            config["headers"] = self.headers
        if self.bearer is not None:
            config["bearer"] = self.bearer
        return config

    def frozen_payload(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "display_name": self.display_name,
            "description": self.description,
            "read_only": self.read_only,
            "transport": self.transport,
            "transport_config": self.transport_config(),
            "source": self.source,
        }

    def approval_summary(self) -> str:
        lines = [
            f"准备安装 MCP Server `{self.server_id}`（{self.display_name}）",
            f"Transport：{self.transport}",
            f"只读声明：{self.read_only}",
            f"来源：{self.source}",
        ]
        if self.transport == "stdio":
            lines.extend([
                f"Executable：{self.command}",
                "Arguments：" + json.dumps(self.args, ensure_ascii=False),
                f"Working directory：{self.cwd or '(未指定，使用该 Server 自己的默认目录)'}",
            ])
            if self.env:
                lines.append(
                    "Env：" + json.dumps(
                        {key: "***" for key in self.env},
                        ensure_ascii=False,
                    )
                )
        else:
            lines.append(f"URL：{self._display_url()}")
            if self.headers:
                lines.append(
                    "Headers：" + json.dumps(
                        {key: "***" for key in self.headers},
                        ensure_ascii=False,
                    )
                )
            if self.bearer is not None:
                lines.append("Bearer：***")
            lines.append(f"Timeout：{self.timeout_seconds}s")
        return "\n".join(lines)

    def _display_url(self) -> str:
        parsed = urlsplit(self.url or "")
        if not parsed.query:
            return self.url or ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?<redacted>"


def create_mcp_install_proposal_spec(
    service: McpApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: McpInstallProposalInput,
    ) -> ControlApprovalProposal | dict[str, Any]:
        existing = await service.registry.get(input_data.server_id)
        if existing is not None:
            return {
                "ok": False,
                "code": "MCP_SERVER_ALREADY_REGISTERED",
                "data": {
                    "server_id": existing.id,
                    "enabled": existing.enabled,
                    "revision": existing.revision,
                },
                "error": f"MCP Server 已注册: {existing.id}",
                "hint": "先诊断现有登记；安装不会隐式覆盖配置。",
            }
        return ControlApprovalProposal(
            action=MCP_INSTALL_ACTION,
            payload=input_data.frozen_payload(),
            summary=input_data.approval_summary(),
            risk=(
                "批准后会保存配置并启动该外部 MCP Server。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_MCP_INSTALL,
        description=(
            "在用户要求安装 MCP Server 时，整理完整配置并提交待确认方案。"
            "信息不足时先在普通对话中询问；不得猜测路径、URL、Secret 或未经验证的包名。"
            "stdio：填 command/args/cwd，密钥填 env；"
            "streamable_http：填 url，鉴权信息填 headers 或 bearer，"
            "也可把密钥直接写在 url 的 query 里。"
            "禁止用 user:pass@host 的形式在 url 里带凭据。"
            "启用成功后能力目录更新，load_toolset 之后工具从下一次决策可见。"
            "本工具必须单独调用。"
        ),
        parameters=PydanticParameters(McpInstallProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class McpInstallApprovalHandler:
    action = MCP_INSTALL_ACTION

    def __init__(self, service: McpApplicationService) -> None:
        self._service = service

    async def execute(
        self,
        payload: Mapping[str, Any],
    ) -> ControlApprovalExecution:
        data = _approval_payload(
            payload,
            {
                "server_id",
                "display_name",
                "description",
                "read_only",
                "transport",
                "transport_config",
                "source",
            },
        )
        record = await self._service.upsert_server(
            server_id=data["server_id"],
            display_name=data["display_name"],
            description=data["description"],
            transport=data["transport"],
            transport_config=data["transport_config"],
            enabled=False,
            read_only=data["read_only"],
        )
        activation = await self._service.test_and_enable(
            record.id,
            expected_revision=record.revision,
        )
        runtime = activation.runtime
        if not activation.succeeded:
            return ControlApprovalExecution(
                succeeded=False,
                message=(
                    f"MCP Server `{record.id}` 已注册但连接测试失败，"
                    "配置保持 disabled。"
                ),
                data={
                    "server_id": record.id,
                    "enabled": False,
                    "revision": record.revision,
                    "runtime": runtime.to_dict(),
                },
            )
        enabled = activation.record
        return ControlApprovalExecution(
            succeeded=True,
            message=(
                f"MCP Server `{enabled.id}` 安装、测试并启用成功。"
                "能力目录已更新，load_toolset 之后工具从下一次决策可见。"
            ),
            data={
                "server_id": enabled.id,
                "enabled": True,
                "revision": enabled.revision,
                "runtime": runtime.to_dict(),
            },
        )


def create_mcp_update_proposal_spec(
    service: McpApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: McpInstallProposalInput,
    ) -> ControlApprovalProposal | dict[str, Any]:
        existing = await service.registry.get(input_data.server_id)
        if existing is None:
            return {
                "ok": False,
                "code": "MCP_SERVER_NOT_FOUND",
                "data": {"server_id": input_data.server_id},
                "error": f"MCP Server 未注册: {input_data.server_id}",
                "hint": "新增 Server 应走 propose_mcp_install。",
            }
        return ControlApprovalProposal(
            action=MCP_UPDATE_ACTION,
            payload={
                **input_data.frozen_payload(),
                "expected_revision": existing.revision,
            },
            summary=(
                f"准备更新 MCP Server `{existing.id}`\n"
                f"当前 Revision：{existing.revision}\n"
                f"{input_data.approval_summary()}"
            ),
            risk=(
                "批准后用新配置替换当前启动配置并真实连接测试；"
                "测试失败时新配置保持 disabled。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_MCP_UPDATE,
        description=(
            "在诊断证明已登记 MCP Server 的配置需要变化时，"
            "提交新配置的更新审批；若提交后该 Server 的登记又有变化，批准时会失败，需要重新提交。"
            "不得用于单纯重连；本工具必须单独调用。"
        ),
        parameters=PydanticParameters(McpInstallProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class McpUpdateApprovalHandler:
    action = MCP_UPDATE_ACTION

    def __init__(self, service: McpApplicationService) -> None:
        self._service = service

    async def execute(
        self,
        payload: Mapping[str, Any],
    ) -> ControlApprovalExecution:
        data = _approval_payload(payload, {
            "server_id",
            "display_name",
            "description",
            "read_only",
            "transport",
            "transport_config",
            "source",
            "expected_revision",
        })
        try:
            record = await self._service.update_server(
                server_id=data["server_id"],
                expected_revision=data["expected_revision"],
                display_name=data["display_name"],
                description=data["description"],
                transport=data["transport"],
                transport_config=data["transport_config"],
                read_only=data["read_only"],
            )
        except McpRecoveryPreconditionError as exc:
            return ControlApprovalExecution(
                False,
                f"MCP 更新条件已变化，未执行：{exc}",
            )
        activation = await self._service.test_and_enable(
            record.id,
            expected_revision=record.revision,
        )
        return ControlApprovalExecution(
            activation.succeeded,
            (
                f"MCP Server `{record.id}` 已更新并启用。"
                if activation.succeeded
                else f"MCP Server `{record.id}` 已更新，但连接测试失败，保持 disabled。"
            ),
            {
                "server_id": record.id,
                "revision": activation.record.revision,
                "enabled": activation.record.enabled,
                "runtime": activation.runtime.to_dict(),
            },
        )


class McpRecoveryProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str


def create_mcp_recovery_proposal_spec(
    service: McpApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: McpRecoveryProposalInput,
    ) -> ControlApprovalProposal | dict[str, Any]:
        record = await service.registry.get(input_data.server_id)
        if record is None:
            return {
                "ok": False,
                "code": "MCP_SERVER_NOT_FOUND",
                "data": {"server_id": input_data.server_id},
                "error": f"未注册 MCP Server `{input_data.server_id}`",
                "hint": "先调用 list_mcp_servers 核对状态；确需新增时提交安装方案。",
            }
        return ControlApprovalProposal(
            action=MCP_RECOVER_ACTION,
            payload={
                "server_id": record.id,
                "expected_revision": record.revision,
            },
            summary=(
                f"准备恢复 MCP Server `{record.id}`（{record.display_name}）\n"
                f"登记状态：{'enabled' if record.enabled else 'disabled'}\n"
                f"Revision：{record.revision}"
            ),
            risk=(
                "批准后会按已登记的配置启动该外部 MCP Server 进行测试；"
                "测试成功后持久启用。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_MCP_RECOVERY,
        description=(
            "按已注册 MCP Server 的当前配置提交重测与重连审批，"
            "不根据 enabled 推断健康。"
            "应先用 list_mcp_servers / test_mcp_server 获取事实；"
            "不得把 TOOLSET_NOT_FOUND 直接解释为未安装。"
            "本工具必须单独调用。"
        ),
        parameters=PydanticParameters(McpRecoveryProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class McpRecoveryApprovalHandler:
    action = MCP_RECOVER_ACTION

    def __init__(self, service: McpApplicationService) -> None:
        self._service = service

    async def execute(
        self,
        payload: Mapping[str, Any],
    ) -> ControlApprovalExecution:
        data = _approval_payload(
            payload,
            {"server_id", "expected_revision"},
        )
        server_id = data["server_id"]
        expected_revision = data["expected_revision"]
        if type(server_id) is not str or type(expected_revision) is not int:
            raise McpInputError("MCP recovery approval payload 类型无效")
        try:
            activation = await self._service.test_and_enable(
                server_id,
                expected_revision=expected_revision,
            )
        except McpRecoveryPreconditionError as exc:
            return ControlApprovalExecution(
                succeeded=False,
                message=f"MCP Server `{server_id}` 恢复条件已变化，未执行：{exc}",
                data={"server_id": server_id, "enabled": False},
            )
        runtime = activation.runtime
        if not activation.succeeded:
            return ControlApprovalExecution(
                succeeded=False,
                message=(
                    f"MCP Server `{server_id}` 连接测试失败，"
                    "配置保持 disabled。"
                ),
                data={
                    "server_id": server_id,
                    "enabled": False,
                    "revision": activation.record.revision,
                    "runtime": runtime.to_dict(),
                },
            )
        return ControlApprovalExecution(
            succeeded=True,
            message=(
                f"MCP Server `{server_id}` 测试并启用成功。"
                "能力目录已更新，load_toolset 之后工具从下一次决策可见。"
            ),
            data={
                "server_id": server_id,
                "enabled": True,
                "revision": activation.record.revision,
                "runtime": runtime.to_dict(),
            },
        )


class McpRemoveProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str


def create_mcp_remove_proposal_spec(
    service: McpApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: McpRemoveProposalInput,
    ) -> ControlApprovalProposal | dict[str, Any]:
        record = await service.registry.get(input_data.server_id)
        if record is None:
            return {
                "ok": False,
                "code": "MCP_SERVER_NOT_FOUND",
                "data": {"server_id": input_data.server_id},
                "error": f"未注册 MCP Server `{input_data.server_id}`",
                "hint": "先调用 list_mcp_servers 核对精确 ID。",
            }
        return ControlApprovalProposal(
            action=MCP_REMOVE_ACTION,
            payload={
                "server_id": record.id,
                "expected_revision": record.revision,
            },
            summary=(
                f"准备删除 MCP Server `{record.id}`（{record.display_name}）\n"
                f"登记状态：{'enabled' if record.enabled else 'disabled'}\n"
                f"Revision：{record.revision}"
            ),
            risk=(
                "批准后将移除该 MCP Server 的登记与本地 Secret；"
                "此后能力目录中不再出现该 Server。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_MCP_REMOVE,
        description=(
            "移除一个已登记 MCP Server 的登记与本地 Secret。"
            "应先用 list_mcp_servers 核对精确 ID。"
            "本工具必须单独调用。"
        ),
        parameters=PydanticParameters(McpRemoveProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class McpRemoveApprovalHandler:
    action = MCP_REMOVE_ACTION

    def __init__(self, service: McpApplicationService) -> None:
        self._service = service

    async def execute(
        self,
        payload: Mapping[str, Any],
    ) -> ControlApprovalExecution:
        data = _approval_payload(
            payload,
            {"server_id", "expected_revision"},
        )
        server_id = data["server_id"]
        expected_revision = data["expected_revision"]
        if type(server_id) is not str or type(expected_revision) is not int:
            raise McpInputError("MCP remove approval payload 类型无效")
        try:
            record = await self._service.remove_server(
                server_id,
                expected_revision=expected_revision,
            )
        except McpRecoveryPreconditionError as exc:
            return ControlApprovalExecution(
                succeeded=False,
                message=f"MCP Server `{server_id}` 删除条件已变化，未执行：{exc}",
                data={"server_id": server_id},
            )
        return ControlApprovalExecution(
            succeeded=True,
            message=(
                f"MCP Server `{record.id}` 已删除。"
                "登记与本地 Secret 已清除，能力目录已更新，"
                "load_toolset 之后工具从下一次决策不再可见。"
            ),
            data={
                "server_id": record.id,
                "revision": record.revision,
            },
        )


def _approval_payload(
    payload: Mapping[str, Any],
    expected: set[str],
) -> dict[str, Any]:
    if set(payload) != expected:
        raise McpInputError("MCP approval payload 字段不匹配")
    return dict(payload)
