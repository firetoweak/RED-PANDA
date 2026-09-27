from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Mapping

from helperme.paths import HelperMeHome
from helperme.mcp.models import (
    McpServerRecord,
    RuntimeAvailability,
    utc_now,
)


class McpRegistry:
    """MCP Server 安装配置的持久读写。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._path = self._root / "servers.json"
        self._lock = asyncio.Lock()

    @classmethod
    def from_home(cls, home: HelperMeHome) -> "McpRegistry":
        return cls(home.mcp_root)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def path(self) -> Path:
        return self._path

    async def list_servers(self) -> tuple[McpServerRecord, ...]:
        async with self._lock:
            return self._read_unlocked()

    def snapshot(self) -> tuple[McpServerRecord, ...]:
        """同步读取一次文件快照，供 Toolset 目录投影。"""
        return self._read_unlocked()

    async def get(self, server_id: str) -> McpServerRecord | None:
        async with self._lock:
            return self._index_unlocked().get(server_id)

    async def replace_all(
        self,
        records: Mapping[str, McpServerRecord],
    ) -> tuple[McpServerRecord, ...]:
        async with self._lock:
            ordered = tuple(
                sorted(records.values(), key=lambda item: item.id)
            )
            self._write_unlocked(ordered)
            return ordered

    async def upsert(self, record: McpServerRecord) -> McpServerRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.get(record.id)
            if existing is None:
                stored = record
            else:
                stored = McpServerRecord(
                    id=record.id,
                    display_name=record.display_name,
                    description=record.description,
                    transport=record.transport,
                    transport_config=record.transport_config,
                    enabled=record.enabled,
                    read_only=record.read_only,
                    revision=existing.revision + 1,
                    created_at=existing.created_at,
                    updated_at=utc_now(),
                )
            index[record.id] = stored
            self._write_unlocked(
                tuple(sorted(index.values(), key=lambda item: item.id))
            )
            return stored

    async def set_enabled(
        self,
        server_id: str,
        enabled: bool,
    ) -> McpServerRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.get(server_id)
            if existing is None:
                raise KeyError(server_id)
            if existing.enabled == enabled:
                return existing
            updated = McpServerRecord(
                id=existing.id,
                display_name=existing.display_name,
                description=existing.description,
                transport=existing.transport,
                transport_config=existing.transport_config,
                enabled=enabled,
                read_only=existing.read_only,
                revision=existing.revision + 1,
                created_at=existing.created_at,
                updated_at=utc_now(),
                last_status=existing.last_status,
                last_checked_at=existing.last_checked_at,
                last_error_summary=existing.last_error_summary,
            )
            index[server_id] = updated
            self._write_unlocked(
                tuple(sorted(index.values(), key=lambda item: item.id))
            )
            return updated

    async def mark_tested(
        self,
        server_id: str,
        status: RuntimeAvailability,
        *,
        error_summary: str = "",
    ) -> McpServerRecord:
        """持久化最近一次测试结果，不改变配置 revision。"""
        async with self._lock:
            index = self._index_unlocked()
            existing = index.get(server_id)
            if existing is None:
                raise KeyError(server_id)
            updated = McpServerRecord(
                id=existing.id,
                display_name=existing.display_name,
                description=existing.description,
                transport=existing.transport,
                transport_config=existing.transport_config,
                enabled=existing.enabled,
                read_only=existing.read_only,
                revision=existing.revision,
                created_at=existing.created_at,
                updated_at=utc_now(),
                last_status=status,
                last_checked_at=utc_now(),
                last_error_summary=error_summary,
            )
            index[server_id] = updated
            self._write_unlocked(
                tuple(sorted(index.values(), key=lambda item: item.id))
            )
            return updated

    async def remove(self, server_id: str) -> McpServerRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.pop(server_id, None)
            if existing is None:
                raise KeyError(server_id)
            self._write_unlocked(
                tuple(sorted(index.values(), key=lambda item: item.id))
            )
            return existing

    def _index_unlocked(self) -> dict[str, McpServerRecord]:
        return {record.id: record for record in self._read_unlocked()}

    def _read_unlocked(self) -> tuple[McpServerRecord, ...]:
        if not self._path.exists():
            return ()
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "servers"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise ValueError("servers.json envelope 格式无效")
        servers = payload["servers"]
        if not isinstance(servers, list):
            raise ValueError("servers.json 格式无效")
        if any(not isinstance(item, dict) for item in servers):
            raise ValueError("servers.json server 必须是 object")
        records = tuple(McpServerRecord.from_dict(item) for item in servers)
        ids = [record.id for record in records]
        if len(ids) != len(set(ids)):
            raise ValueError("servers.json 包含重复 id")
        return records

    def _write_unlocked(self, records: tuple[McpServerRecord, ...]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "servers": [record.to_dict() for record in records],
        }
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)
