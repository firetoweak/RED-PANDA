from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.types import InputRequiredResult

from helperme.tools.spec import ToolSpec
from helperme.mcp.toolsets import (
    ToolsetDescriptor,
    ToolsetLoadError,
)
from helperme.mcp.adapter import (
    adapt_call_result,
    adapt_transport_error,
    build_parameters,
    build_output_validator,
    encode_tool_name,
    ensure_unique_encoded_names,
    input_required_unsupported,
    parse_toolset_id,
)
from helperme.mcp.client_manager import McpClientError, McpClientManager
from helperme.mcp.registry import McpRegistry


@dataclass(frozen=True, slots=True)
class McpDiscoveredTool:
    spec: ToolSpec
    provider_data: Mapping[str, object]


class McpToolsetProvider:
    """目录只读 Registry；真正连接发生在 tool_specs / 工具调用。"""

    def __init__(
        self,
        registry: McpRegistry,
        client_manager: McpClientManager,
    ) -> None:
        self._registry = registry
        self._client_manager = client_manager

    def descriptors(self, *, read_only_only: bool = False) -> tuple[ToolsetDescriptor, ...]:
        records = tuple(
            record for record in self._registry.snapshot()
            if record.enabled and (not read_only_only or record.read_only)
        )
        descriptors: list[ToolsetDescriptor] = []
        for record in records:
            description = f"{record.display_name}: {record.description}".rstrip(": ")
            descriptors.append(
                ToolsetDescriptor(
                    id=record.toolset_id,
                    description=description,
                    revision=record.revision,
                )
            )
        return tuple(descriptors)

    async def tool_specs(self, toolset_id: str) -> tuple[ToolSpec, ...]:
        return tuple(
            item.spec for item in await self.discover_tools(toolset_id)
        )

    async def discover_tools(
        self,
        toolset_id: str,
    ) -> tuple[McpDiscoveredTool, ...]:
        server_id = parse_toolset_id(toolset_id)
        record = await self._registry.get(server_id)
        if record is None or not record.enabled:
            raise ToolsetLoadError(
                "TOOLSET_NOT_FOUND",
                f"Toolset {toolset_id} not found",
                hint="请从可选 Toolset 目录中选择有效 ID。",
                data={"toolset_id": toolset_id},
            )
        try:
            tools = await self._client_manager.list_tools(record)
        except asyncio.CancelledError:
            raise
        except (
            TimeoutError,
            OSError,
            McpClientError,
            anyio.EndOfStream,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ) as exc:
            summary = self._client_manager.sanitized_error(record, exc)
            self._client_manager.runtime_state(server_id).mark_unavailable(summary)
            await self._client_manager.invalidate(server_id)
            raise ToolsetLoadError(
                "MCP_TRANSPORT_ERROR",
                summary,
                hint="检查 Server 是否可用后重试 load_toolset。",
                data={"server_id": server_id},
            ) from exc

        discovered: list[McpDiscoveredTool] = []
        for tool in tools:
            parameters = build_parameters(tool.name, tool.input_schema)
            encoded = encode_tool_name(record.id, tool.name)
            output_validator = build_output_validator(
                tool.name,
                tool.output_schema,
            )
            handler = self._make_handler(
                record_id=record.id,
                expected_revision=record.revision,
                tool_name=tool.name,
                output_validator=output_validator,
            )
            spec = ToolSpec(
                name=encoded,
                description=tool.description or tool.name,
                parameters=parameters,
                handler=handler,
            )
            discovered.append(
                McpDiscoveredTool(
                    spec,
                    {
                        "tool_name": tool.name,
                        "output_schema": (
                            None
                            if tool.output_schema is None
                            else dict(tool.output_schema)
                        ),
                    },
                )
            )
        specs = [item.spec for item in discovered]
        ensure_unique_encoded_names(specs)
        return tuple(discovered)

    def restore_spec(
        self,
        *,
        toolset_id: str,
        revision: int,
        name: str,
        description: str,
        parameters: Mapping[str, object],
        requires_authorization: bool,
        provider_data: Mapping[str, object],
    ) -> ToolSpec:
        if set(provider_data) != {"tool_name", "output_schema"}:
            raise ValueError("MCP tool provider_data 字段不匹配")
        tool_name = provider_data["tool_name"]
        output_schema = provider_data["output_schema"]
        if type(tool_name) is not str or not tool_name:
            raise ValueError("MCP tool_name 必须是非空字符串")
        if output_schema is not None and not isinstance(output_schema, Mapping):
            raise ValueError("MCP output_schema 必须是 object 或 null")
        server_id = parse_toolset_id(toolset_id)
        if encode_tool_name(server_id, tool_name) != name:
            raise ValueError("MCP 工具快照名称与绑定身份不一致")
        return ToolSpec(
            name=name,
            description=description,
            parameters=build_parameters(tool_name, parameters),
            handler=self._make_handler(
                record_id=server_id,
                expected_revision=revision,
                tool_name=tool_name,
                output_validator=build_output_validator(
                    tool_name,
                    output_schema,
                ),
            ),
            requires_authorization=requires_authorization,
        )

    def _make_handler(
        self,
        *,
        record_id: str,
        expected_revision: int,
        tool_name: str,
        output_validator: Any | None,
    ):
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            record = await self._registry.get(record_id)
            if record is None or not record.enabled:
                return {
                    "ok": False,
                    "code": "MCP_SERVER_DISABLED",
                    "data": {"server_id": record_id},
                    "error": f"MCP Server 不可用或已停用: {record_id}",
                    "hint": "请用户通过 /mcp 重新启用该 Server。",
                }
            if record.revision != expected_revision:
                return {
                    "ok": False,
                    "code": "MCP_SERVER_CHANGED",
                    "data": {
                        "server_id": record_id,
                        "loaded_revision": expected_revision,
                        "current_revision": record.revision,
                    },
                    "error": "MCP Server 配置已变化，当前 Toolset 快照已过期",
                    "hint": "请在新的 Step 中重新加载该 Toolset。",
                }
            try:
                result = await self._client_manager.call_tool(
                    record,
                    tool_name,
                    arguments,
                )
            except asyncio.CancelledError:
                raise
            except (
                TimeoutError,
                OSError,
                McpClientError,
                anyio.EndOfStream,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
            ) as exc:
                summary = self._client_manager.sanitized_error(record, exc)
                self._client_manager.runtime_state(record_id).mark_unavailable(
                    summary
                )
                await self._client_manager.invalidate(record_id)
                return adapt_transport_error(exc, error_summary=summary)
            if isinstance(result, InputRequiredResult):
                return input_required_unsupported()

            return adapt_call_result(
                result,
                output_validator=output_validator,
                secret_values=self._client_manager.secret_values(record),
            )

        return handler
