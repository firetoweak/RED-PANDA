from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from redpanda.mcp.client_manager import McpClientError, McpClientManager
from redpanda.mcp.content import McpContentService
from redpanda.mcp.errors import (
    McpConfigurationError,
    McpRecoveryPreconditionError,
    McpServerNotFoundError,
)
from redpanda.mcp.models import (
    QUERY_SECRET_KEY,
    McpServerRecord,
    McpServerRuntimeState,
    RuntimeAvailability,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    TransportKind,
    utc_now,
    validate_server_id,
)
from redpanda.mcp.registry import McpRegistry
from redpanda.mcp.secrets import McpSecretStore
from redpanda.mcp.toolset_provider import McpToolsetProvider


@dataclass(frozen=True)
class ServerSummary:
    record: McpServerRecord
    runtime: McpServerRuntimeState | None = None

    def to_dict(self, *, include_runtime: bool = False) -> dict[str, Any]:
        payload = self.record.to_dict()
        if include_runtime and self.runtime is not None:
            payload["runtime"] = self.runtime.to_dict()
        return payload


@dataclass(frozen=True)
class ServerActivationResult:
    record: McpServerRecord
    runtime: McpServerRuntimeState

    @property
    def succeeded(self) -> bool:
        return self.runtime.status is RuntimeAvailability.AVAILABLE


class McpApplicationService:
    """用户控制面：list/upsert/enable/remove/test。"""

    def __init__(
        self,
        registry: McpRegistry,
        secret_store: McpSecretStore,
        client_manager: McpClientManager,
        content_service: McpContentService | None = None,
    ) -> None:
        self.registry = registry
        self.secret_store = secret_store
        self.client_manager = client_manager
        self.content = (
            McpContentService(registry, client_manager)
            if content_service is None
            else content_service
        )
        self.toolset_provider = McpToolsetProvider(registry, client_manager)
        self._management_lock = asyncio.Lock()

    async def list_servers(
        self,
        *,
        include_runtime: bool = False,
    ) -> list[ServerSummary]:
        records = await self.registry.list_servers()
        return [
            ServerSummary(
                record=record,
                runtime=(
                    self.client_manager.runtime_state(record.id)
                    if include_runtime
                    else None
                ),
            )
            for record in records
        ]

    async def upsert_server(
        self,
        *,
        server_id: str,
        display_name: str,
        description: str = "",
        transport: str,
        transport_config: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
        enabled: bool = False,
        read_only: bool = False,
    ) -> McpServerRecord:
        async with self._management_lock:
            return await self._upsert_server_locked(
                server_id=server_id,
                display_name=display_name,
                description=description,
                transport=transport,
                transport_config=transport_config,
                secrets=secrets,
                enabled=enabled,
                read_only=read_only,
            )

    async def update_server(
        self,
        *,
        server_id: str,
        expected_revision: int,
        display_name: str,
        description: str,
        transport: str,
        transport_config: Mapping[str, Any],
        read_only: bool = False,
    ) -> McpServerRecord:
        async with self._management_lock:
            current = await self.registry.get(server_id)
            if current is None:
                raise McpRecoveryPreconditionError(
                    f"MCP Server 不存在: {server_id}"
                )
            if current.revision != expected_revision:
                raise McpRecoveryPreconditionError(
                    f"MCP Server `{server_id}` 配置已变化："
                    f"expected revision {expected_revision}, "
                    f"current revision {current.revision}"
                )
            return await self._upsert_server_locked(
                server_id=server_id,
                display_name=display_name,
                description=description,
                transport=transport,
                transport_config=transport_config,
                secrets=None,
                enabled=False,
                read_only=read_only,
            )

    async def _upsert_server_locked(
        self,
        *,
        server_id: str,
        display_name: str,
        description: str,
        transport: str,
        transport_config: Mapping[str, Any],
        secrets: Mapping[str, str] | None,
        enabled: bool,
        read_only: bool,
    ) -> McpServerRecord:
        existing = await self.registry.get(server_id)
        try:
            kind, prepared_secrets, config = self._validated_configuration(
                server_id=server_id,
                transport=transport,
                transport_config=transport_config,
                secrets=secrets,
                existing=existing,
            )
            record = McpServerRecord(
                id=server_id,
                display_name=display_name,
                description=description,
                transport=kind,
                transport_config=config,
                enabled=enabled,
                read_only=read_only,
                created_at=(
                    existing.created_at if existing is not None else utc_now()
                ),
                updated_at=utc_now(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise McpConfigurationError(str(exc)) from exc

        updated_secrets = bool(prepared_secrets)
        previous_secrets = self.secret_store.snapshot_namespace(server_id)
        if updated_secrets:
            # 先写 Secret，再原子替换 Registry；失败则恢复旧命名空间。
            self.secret_store.put_namespace(server_id, prepared_secrets)
        try:
            stored = await self.registry.upsert(record)
        except BaseException as registry_error:
            if updated_secrets:
                try:
                    if previous_secrets:
                        self.secret_store.put_namespace(
                            server_id,
                            previous_secrets,
                        )
                    else:
                        self.secret_store.delete_namespace(server_id)
                except BaseException as rollback_error:
                    raise BaseExceptionGroup(
                        "MCP Registry 写入失败且 Secret 回滚失败",
                        [registry_error, rollback_error],
                    )
            raise
        await self.client_manager.invalidate(server_id)
        return stored

    async def set_server_enabled(
        self,
        server_id: str,
        enabled: bool,
    ) -> McpServerRecord:
        async with self._management_lock:
            if await self.registry.get(server_id) is None:
                raise McpServerNotFoundError(
                    f"MCP Server 不存在: {server_id}"
                )
            record = await self.registry.set_enabled(server_id, enabled)
            await self.client_manager.invalidate(server_id)
            return record

    async def remove_server(
        self,
        server_id: str,
        *,
        expected_revision: int | None = None,
    ) -> McpServerRecord:
        async with self._management_lock:
            current = await self.registry.get(server_id)
            if current is None:
                if expected_revision is None:
                    raise McpServerNotFoundError(
                        f"MCP Server 不存在: {server_id}"
                    )
                raise McpRecoveryPreconditionError(
                    f"MCP Server 不存在: {server_id}"
                )
            if (
                expected_revision is not None
                and current.revision != expected_revision
            ):
                raise McpRecoveryPreconditionError(
                    f"MCP Server `{server_id}` 配置已变化："
                    f"expected revision {expected_revision}, "
                    f"current revision {current.revision}"
                )
            record = await self.registry.remove(server_id)
            self.secret_store.delete_namespace(server_id)
            await self.client_manager.invalidate(server_id)
            return record

    async def test_server(self, server_id: str) -> McpServerRuntimeState:
        record = await self.registry.get(server_id)
        if record is None:
            raise McpServerNotFoundError(f"MCP Server 不存在: {server_id}")
        runtime = await self._test_record(record)
        await self._persist_test_result(server_id, runtime)
        return runtime

    async def test_and_enable(
        self,
        server_id: str,
        *,
        expected_revision: int | None = None,
    ) -> ServerActivationResult:
        """测试冻结配置，并仅在测试成功后原子推进到 enabled。"""
        async with self._management_lock:
            record = await self.registry.get(server_id)
            if record is None:
                raise McpRecoveryPreconditionError(
                    f"MCP Server 不存在: {server_id}"
                )
            if (
                expected_revision is not None
                and record.revision != expected_revision
            ):
                raise McpRecoveryPreconditionError(
                    f"MCP Server `{server_id}` 配置已变化："
                    f"expected revision {expected_revision}, "
                    f"current revision {record.revision}"
                )
            runtime = await self._test_record(record)
            tested_record = await self._persist_test_result(server_id, runtime)
            if runtime.status is not RuntimeAvailability.AVAILABLE:
                return ServerActivationResult(tested_record, runtime)
            enabled = await self.registry.set_enabled(server_id, True)
            await self.client_manager.invalidate(server_id)
            return ServerActivationResult(enabled, runtime)

    async def _persist_test_result(
        self,
        server_id: str,
        runtime: McpServerRuntimeState,
    ) -> McpServerRecord:
        return await self.registry.mark_tested(
            server_id,
            runtime.status,
            error_summary=runtime.last_error_summary or "",
        )

    async def _test_record(
        self,
        record: McpServerRecord,
    ) -> McpServerRuntimeState:
        try:
            return await self.client_manager.test_connection(record)
        except (TimeoutError, OSError, McpClientError) as exc:
            state = self.client_manager.runtime_state(record.id)
            state.mark_unavailable(
                self.client_manager.sanitized_error(record, exc)
            )
            return state

    def _validated_configuration(
        self,
        *,
        server_id: str,
        transport: str,
        transport_config: Mapping[str, Any],
        secrets: Mapping[str, str] | None,
        existing: McpServerRecord | None,
    ) -> tuple[
        TransportKind,
        dict[str, str],
        StdioTransportConfig | StreamableHttpTransportConfig,
    ]:
        validate_server_id(server_id)
        if type(transport) is not str:
            raise TypeError("MCP transport 必须是 string")
        if not isinstance(transport_config, Mapping):
            raise TypeError("MCP transport_config 必须是 mapping")
        kind = TransportKind(transport)
        if secrets is None:
            prepared_secrets: dict[str, str] = {}
        elif isinstance(secrets, Mapping) and all(
            type(key) is str and type(value) is str
            for key, value in secrets.items()
        ):
            prepared_secrets = dict(secrets)
        else:
            raise TypeError("MCP secrets 必须是字符串映射")
        if kind is TransportKind.STDIO:
            unknown = set(transport_config) - {"command", "args", "cwd", "env"}
            if unknown:
                raise ValueError(
                    f"stdio transport_config 包含未知字段: {sorted(unknown)}"
                )
            command = transport_config["command"]
            args = transport_config.get("args", [])
            cwd = transport_config.get("cwd")
            env = transport_config.get("env", {})
            if type(command) is not str:
                raise TypeError("stdio command 必须是 string")
            if type(args) not in (list, tuple) or any(
                type(argument) is not str for argument in args
            ):
                raise TypeError("stdio args 必须是 string array")
            if cwd is not None and type(cwd) is not str:
                raise TypeError("stdio cwd 必须是 string|null")
            if not isinstance(env, Mapping) or any(
                type(key) is not str or type(value) is not str
                for key, value in env.items()
            ):
                raise TypeError("stdio env 必须是 string mapping")
            prepared_secrets.update(env)
            refs = self._resolve_refs(
                server_id=server_id,
                existing=existing,
                prepared_secrets=prepared_secrets,
                existing_refs=(
                    dict(existing.transport_config.env_refs)
                    if existing is not None
                    and isinstance(
                        existing.transport_config,
                        StdioTransportConfig,
                    )
                    else {}
                ),
            )
            config: StdioTransportConfig | StreamableHttpTransportConfig = (
                StdioTransportConfig(
                    command=command,
                    args=tuple(args),
                    cwd=cwd,
                    env_refs=refs,
                )
            )
        else:
            unknown = set(transport_config) - {
                "url",
                "headers",
                "bearer",
                "timeout_seconds",
            }
            if unknown:
                raise ValueError(
                    "streamable_http transport_config 包含未知字段: "
                    f"{sorted(unknown)}"
                )
            url = transport_config["url"]
            headers = transport_config.get("headers", {})
            bearer = transport_config.get("bearer")
            timeout = transport_config.get("timeout_seconds", 30)
            if type(url) is not str:
                raise TypeError("streamable_http url 必须是 string")
            if not isinstance(headers, Mapping) or any(
                type(key) is not str or type(value) is not str
                for key, value in headers.items()
            ):
                raise TypeError("HTTP headers 必须是 string mapping")
            if bearer is not None and type(bearer) is not str:
                raise TypeError("HTTP bearer 必须是 string|null")
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise TypeError("HTTP timeout_seconds 必须是 number")
            prepared_secrets.update(headers)
            if bearer is not None:
                if not bearer:
                    raise ValueError("HTTP bearer 不能为空")
                prepared_secrets["Authorization"] = f"Bearer {bearer}"
            parsed_url = urlsplit(url)
            if parsed_url.query:
                prepared_secrets[QUERY_SECRET_KEY] = parsed_url.query
                url = urlunsplit(
                    (
                        parsed_url.scheme,
                        parsed_url.netloc,
                        parsed_url.path,
                        "",
                        "",
                    )
                )
            existing_header_refs = (
                dict(existing.transport_config.header_refs)
                if existing is not None
                and isinstance(
                    existing.transport_config,
                    StreamableHttpTransportConfig,
                )
                else {}
            )
            existing_query_refs = (
                dict(existing.transport_config.query_refs)
                if existing is not None
                and isinstance(
                    existing.transport_config,
                    StreamableHttpTransportConfig,
                )
                else {}
            )
            refs = self._resolve_refs(
                server_id=server_id,
                existing=existing,
                prepared_secrets=prepared_secrets,
                existing_refs={**existing_header_refs, **existing_query_refs},
            )
            query_refs: dict[str, str] = {}
            if QUERY_SECRET_KEY in refs:
                query_refs = {QUERY_SECRET_KEY: refs.pop(QUERY_SECRET_KEY)}
            config = StreamableHttpTransportConfig(
                url=url,
                header_refs=refs,
                query_refs=query_refs,
                timeout_seconds=float(timeout),
            )
        return kind, prepared_secrets, config

    def _resolve_refs(
        self,
        *,
        server_id: str,
        existing: McpServerRecord | None,
        prepared_secrets: Mapping[str, str],
        existing_refs: Mapping[str, str],
    ) -> dict[str, str]:
        if prepared_secrets:
            return {
                key: self.secret_store.ref_for(server_id, key)
                for key in prepared_secrets
            }
        if existing is not None:
            return dict(existing_refs)
        return {}
