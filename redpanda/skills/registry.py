from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from redpanda.skills.models import SkillRecord, utc_now


class SkillRegistry:
    """已安装 Skill 的持久事实源。"""

    def __init__(self, skills_root: Path) -> None:
        self._root = skills_root.resolve()
        self._path = self._root / "registry.json"
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> tuple[SkillRecord, ...]:
        """读取一次原子文件快照，供 Decision Context 投影 Catalog。"""
        return self._read_unlocked()

    async def list_skills(self) -> tuple[SkillRecord, ...]:
        async with self._lock:
            return self._read_unlocked()

    async def get(self, skill_id: str) -> SkillRecord | None:
        async with self._lock:
            return self._index_unlocked().get(skill_id)

    async def add(self, record: SkillRecord) -> SkillRecord:
        async with self._lock:
            index = self._index_unlocked()
            if record.name in index:
                raise ValueError(f"Skill 已安装: {record.name}")
            index[record.name] = record
            self._write_unlocked(tuple(sorted(
                index.values(), key=lambda item: item.name
            )))
            return record

    async def set_enabled(self, skill_id: str, enabled: bool) -> SkillRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.get(skill_id)
            if existing is None:
                raise KeyError(skill_id)
            if existing.enabled == enabled:
                return existing
            updated = SkillRecord(
                name=existing.name,
                description=existing.description,
                source=existing.source,
                resolved_ref=existing.resolved_ref,
                content_hash=existing.content_hash,
                enabled=enabled,
                revision=existing.revision + 1,
                created_at=existing.created_at,
                updated_at=utc_now(),
            )
            index[skill_id] = updated
            self._write_unlocked(tuple(sorted(
                index.values(), key=lambda item: item.name
            )))
            return updated

    async def replace(self, record: SkillRecord) -> SkillRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.get(record.name)
            if existing is None:
                raise KeyError(record.name)
            stored = SkillRecord(
                name=record.name,
                description=record.description,
                source=record.source,
                resolved_ref=record.resolved_ref,
                content_hash=record.content_hash,
                enabled=record.enabled,
                revision=existing.revision + 1,
                created_at=existing.created_at,
                updated_at=utc_now(),
            )
            index[record.name] = stored
            self._write_unlocked(tuple(sorted(
                index.values(), key=lambda item: item.name
            )))
            return stored

    async def remove(self, skill_id: str) -> SkillRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.pop(skill_id, None)
            if existing is None:
                raise KeyError(skill_id)
            self._write_unlocked(tuple(sorted(
                index.values(), key=lambda item: item.name
            )))
            return existing

    def _index_unlocked(self) -> dict[str, SkillRecord]:
        return {record.name: record for record in self._read_unlocked()}

    def _read_unlocked(self) -> tuple[SkillRecord, ...]:
        if not self._path.exists():
            return ()
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "skills"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise ValueError("Skill registry.json envelope 格式无效")
        skills = payload["skills"]
        if not isinstance(skills, list):
            raise ValueError("Skill registry.json 格式无效")
        if any(not isinstance(item, dict) for item in skills):
            raise ValueError("Skill registry.json record 必须是 object")
        records = tuple(SkillRecord.from_dict(item) for item in skills)
        names = [item.name for item in records]
        if len(names) != len(set(names)):
            raise ValueError("Skill registry.json 包含重复 name")
        return records

    def _write_unlocked(self, records: tuple[SkillRecord, ...]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "skills": [record.to_dict() for record in records],
        }
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)
