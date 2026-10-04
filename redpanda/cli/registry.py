from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from redpanda.cli.models import CliRecord, utc_now


class CliRegistry:
    """已登记 CLI 的持久事实源。"""

    def __init__(self, clis_root: Path) -> None:
        self._root = clis_root.resolve()
        self._path = self._root / "registry.json"
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> tuple[CliRecord, ...]:
        """读取一次原子文件快照，供 Decision Context 投影目录。"""
        return self._read_unlocked()

    async def list_clis(self) -> tuple[CliRecord, ...]:
        async with self._lock:
            return self._read_unlocked()

    async def get(self, cli_id: str) -> CliRecord | None:
        async with self._lock:
            return self._index_unlocked().get(cli_id)

    async def add(self, record: CliRecord) -> CliRecord:
        async with self._lock:
            index = self._index_unlocked()
            if record.name in index:
                raise ValueError(f"CLI 已登记: {record.name}")
            index[record.name] = record
            self._write_unlocked(tuple(sorted(
                index.values(), key=lambda item: item.name
            )))
            return record

    async def replace(self, record: CliRecord) -> CliRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.get(record.name)
            if existing is None:
                raise KeyError(record.name)
            stored = CliRecord(
                name=record.name,
                description=record.description,
                source=record.source,
                version=record.version,
                resolved_path=record.resolved_path,
                health=record.health,
                revision=existing.revision + 1,
                created_at=existing.created_at,
                updated_at=utc_now(),
            )
            index[record.name] = stored
            self._write_unlocked(tuple(sorted(
                index.values(), key=lambda item: item.name
            )))
            return stored

    async def remove(self, cli_id: str) -> CliRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.pop(cli_id, None)
            if existing is None:
                raise KeyError(cli_id)
            self._write_unlocked(tuple(sorted(
                index.values(), key=lambda item: item.name
            )))
            return existing

    def _index_unlocked(self) -> dict[str, CliRecord]:
        return {record.name: record for record in self._read_unlocked()}

    def _read_unlocked(self) -> tuple[CliRecord, ...]:
        if not self._path.exists():
            return ()
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "clis"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise ValueError("CLI registry.json envelope 格式无效")
        clis = payload["clis"]
        if not isinstance(clis, list):
            raise ValueError("CLI registry.json 格式无效")
        if any(not isinstance(item, dict) for item in clis):
            raise ValueError("CLI registry.json record 必须是 object")
        records = tuple(CliRecord.from_dict(item) for item in clis)
        names = [item.name for item in records]
        if len(names) != len(set(names)):
            raise ValueError("CLI registry.json 包含重复 name")
        return records

    def _write_unlocked(self, records: tuple[CliRecord, ...]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "clis": [record.to_dict() for record in records],
        }
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)
