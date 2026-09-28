from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from helperme.tools.spec import EmptyInput, PydanticParameters, ToolSpec
from helperme.mcp.application import McpApplicationService
from helperme.mcp.models import RuntimeAvailability


class McpServerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str


def create_mcp_management_specs(
    service: McpApplicationService,
) -> tuple[ToolSpec, ...]:
    async def list_servers(_input: EmptyInput) -> dict:
        items = await service.list_servers(include_runtime=False)
        servers = []
        for item in items:
            record = item.record
            servers.append({
                "id": record.id,
                "display_name": record.display_name,
                "description": record.description,
                "transport": record.transport.value,
                "enabled": record.enabled,
                "revision": record.revision,
                "last_status": record.last_status.value,
                "last_checked_at": (
                    record.last_checked_at.isoformat()
                    if record.last_checked_at is not None
                    else None
                ),
                "last_error_summary": record.last_error_summary or None,
            })
        return {
            "ok": True,
            "code": "MCP_SERVERS_LISTED",
            "data": {"servers": servers},
            "error": None,
            "hint": None,
        }

    async def test_server(input_data: McpServerInput) -> dict:
        record = await service.registry.get(input_data.server_id)
        if record is None:
            return {
                "ok": False,
                "code": "MCP_SERVER_NOT_FOUND",
                "data": {"server_id": input_data.server_id},
                "error": f"未注册 MCP Server `{input_data.server_id}`",
                "hint": "先调用 list_mcp_servers 核对精确 ID。",
            }
        runtime = await service.test_server(record.id)
        available = runtime.status is RuntimeAvailability.AVAILABLE
        code = (
            "MCP_SERVER_AVAILABLE"
            if available
            else "MCP_SERVER_UNAVAILABLE"
        )
        return {
            "ok": available,
            "code": code,
            "data": {
                "server_id": record.id,
                "enabled": record.enabled,
                "revision": record.revision,
                "runtime": runtime.to_dict(),
            },
            "error": None if available else runtime.last_error_summary,
            "hint": None if available else (
                "按 error 判断：配置本身有误用 propose_mcp_update；配置没问题只是连不上，用 propose_mcp_recovery。"
            ),
        }

    return (
        ToolSpec(
            name="list_mcp_servers",
            description=(
                "列出所有已注册 MCP Server，包括 disabled 项及最近测试状态。"
                "last_status 是上次真实测试结果（unknown 表示从未测试）。"
                "用户询问某个 MCP 是否安装、可用或可恢复时，应先调用本工具；"
                "Toolset 目录只包含 enabled 项，不能代替管理状态。"
            ),
            parameters=PydanticParameters(EmptyInput),
            handler=list_servers,
        ),
        ToolSpec(
            name="test_mcp_server",
            description=(
                "按已登记的配置真实测试一个 MCP Server，disabled 项也可测试。"
                "失败只证明本次连接不可用，不代表 Server 未安装。"
            ),
            parameters=PydanticParameters(McpServerInput),
            handler=test_server,
        ),
    )
