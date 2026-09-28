from __future__ import annotations

from pathlib import Path, PurePosixPath
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from helperme.tools.spec import PydanticParameters, ToolSpec
from helperme.skills.models import SkillBundle, SkillRecord
from helperme.skills.package import (
    LocalSkillPackageReader,
    SkillPackageError,
    validate_relative_skill_path,
)
from helperme.skills.registry import SkillRegistry


LOAD_SKILL = "load_skill"
READ_SKILL_RESOURCE = "read_skill_resource"


class SkillRuntimeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.data = {} if data is None else data


class LoadSkillInput(BaseModel):
    skill_id: str


class ReadSkillResourceInput(BaseModel):
    skill_id: str
    relative_path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20_000, ge=1, le=50_000)


class SkillToolCatalog:
    """把已启用 Skill 投影为两个普通工具。"""

    def __init__(
        self,
        registry: SkillRegistry,
        package_reader: LocalSkillPackageReader | None = None,
        *,
        max_catalog_chars: int = 20_000,
    ) -> None:
        if max_catalog_chars <= 0:
            raise ValueError("max_catalog_chars 必须大于 0")
        self.registry = registry
        self.package_reader = (
            LocalSkillPackageReader()
            if package_reader is None
            else package_reader
        )
        self.packages_root = registry.root / "packages"
        self.max_catalog_chars = max_catalog_chars

    def tool_specs(
        self,
        catalog: Mapping[str, int] | None = None,
    ) -> list[ToolSpec]:
        records = tuple(
            sorted(
                (
                    record
                    for record in self.registry.snapshot()
                    if record.enabled
                ),
                key=lambda item: item.name,
            )
        )
        rendered_catalog = "\n".join(
            f"- {record.name}: {record.description}"
            for record in records
        )
        if len(rendered_catalog) > self.max_catalog_chars:
            raise RuntimeError("SKILL_CATALOG_LIMIT: 完整 Skill 目录超出预算")
        revisions = (
            {record.name: record.revision for record in records}
            if catalog is None
            else dict(catalog)
        )

        async def load_skill(input_data: LoadSkillInput) -> dict[str, Any]:
            revision = revisions.get(input_data.skill_id)
            if revision is None:
                return _error_result(SkillRuntimeError(
                    "SKILL_NOT_FOUND",
                    f"Skill {input_data.skill_id} 不在当前能力目录中",
                    hint="从上下文中的当前技能目录选择有效 ID。",
                    data={"skill_id": input_data.skill_id},
                ))
            try:
                current = await self._require_current_record(
                    input_data.skill_id,
                    revision,
                )
                package_directory, bundle = self._validated_bundle(current)
            except SkillRuntimeError as exc:
                return _error_result(exc)
            return {
                "ok": True,
                "code": "SKILL_LOADED",
                "data": {
                    "skill_id": current.name,
                    "revision": current.revision,
                    "skill_dir": str(package_directory),
                    "content": bundle.main_instructions,
                },
            }

        async def read_resource(
            input_data: ReadSkillResourceInput,
        ) -> dict[str, Any]:
            revision = revisions.get(input_data.skill_id)
            if revision is None:
                return _error_result(SkillRuntimeError(
                    "SKILL_NOT_FOUND",
                    f"Skill {input_data.skill_id} 不在当前能力目录中",
                    hint="从上下文中的当前技能目录选择有效 ID。",
                    data={"skill_id": input_data.skill_id},
                ))
            try:
                current = await self._require_current_record(
                    input_data.skill_id,
                    revision,
                )
                return self._read_resource(
                    current,
                    input_data.relative_path,
                    input_data.offset,
                    input_data.limit,
                )
            except SkillRuntimeError as exc:
                return _error_result(exc)

        return [
            ToolSpec(
                name=LOAD_SKILL,
                description=(
                    "读取一个适合当前任务的可复用详细指令包，按 ID 返回完整指令。"
                    "必须单独调用，不能与依赖其结果的工具同批执行。\n"
                    "当前技能目录由上下文消息提供。"
                ),
                parameters=PydanticParameters(LoadSkillInput),
                handler=load_skill,
                exclusive_batch=True,
            ),
            ToolSpec(
                name=READ_SKILL_RESOURCE,
                description=(
                    "按字符范围读取当前可用 Skill 的文本资源。"
                    "relative_path 必须是对应 Skill 包内的相对路径。"
                ),
                parameters=PydanticParameters(ReadSkillResourceInput),
                handler=read_resource,
            ),
        ]

    async def _require_current_record(
        self,
        skill_id: str,
        revision: int,
    ) -> SkillRecord:
        current = await self.registry.get(skill_id)
        if (
            current is None
            or not current.enabled
            or current.revision != revision
        ):
            raise SkillRuntimeError(
                "SKILL_CATALOG_STALE",
                f"Skill {skill_id} 在当前能力目录生成后已变化",
                hint="在下一次决策中按最新 Skill 目录重新选择。",
                data={
                    "skill_id": skill_id,
                    "expected_revision": revision,
                    "current_revision": (
                        current.revision if current is not None else None
                    ),
                },
            )
        return current

    def _validated_bundle(
        self,
        record: SkillRecord,
    ) -> tuple[Path, SkillBundle]:
        package_directory = (self.packages_root / record.name).resolve()
        if not package_directory.is_dir():
            raise RuntimeError(f"已登记 Skill 包目录丢失: {record.name}")
        bundle = self.package_reader.read(package_directory)
        if bundle.name != record.name:
            raise RuntimeError(f"Skill 严格身份不一致: {record.name}")
        if bundle.description != record.description:
            raise RuntimeError(
                f"Skill Registry description 与包不一致: {record.name}"
            )
        if bundle.content_hash != record.content_hash:
            raise RuntimeError(f"Skill Registry hash 与包不一致: {record.name}")
        return package_directory, bundle

    def _read_resource(
        self,
        record: SkillRecord,
        relative_path: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        package_directory, _ = self._validated_bundle(record)
        try:
            normalized = validate_relative_skill_path(relative_path)
        except SkillPackageError as exc:
            raise SkillRuntimeError(
                "INVALID_SKILL_RESOURCE_PATH",
                str(exc),
                hint="使用该 Skill 包内的相对路径，参考 load_skill 返回的正文。",
                data={"skill_id": record.name, "relative_path": relative_path},
            ) from exc
        resource = package_directory.joinpath(*PurePosixPath(normalized).parts)
        if not resource.is_file():
            raise SkillRuntimeError(
                "SKILL_RESOURCE_NOT_FOUND",
                f"Skill 资源不存在: {relative_path}",
                hint="根据 SKILL.md 中列出的 supporting file 路径重试。",
                data={"skill_id": record.name, "relative_path": relative_path},
            )
        try:
            content = resource.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillRuntimeError(
                "SKILL_RESOURCE_NOT_TEXT",
                f"Skill 资源不是 UTF-8 文本: {relative_path}",
                hint="二进制 assets 不通过 read_skill_resource 读取。",
            ) from exc
        if offset > len(content):
            raise SkillRuntimeError(
                "SKILL_RESOURCE_OFFSET_OUT_OF_RANGE",
                f"offset={offset} 超出资源长度 {len(content)}",
                hint="使用上一次结果的 next_offset，或从 0 开始。",
            )
        end = min(offset + limit, len(content))
        return {
            "ok": True,
            "code": "SKILL_RESOURCE_READ",
            "data": {
                "skill_id": record.name,
                "revision": record.revision,
                "relative_path": normalized,
                "content": content[offset:end],
                "offset": offset,
                "next_offset": end if end < len(content) else None,
                "total_chars": len(content),
            },
        }


def _error_result(exc: SkillRuntimeError) -> dict[str, Any]:
    return {
        "ok": False,
        "code": exc.code,
        "data": exc.data or None,
        "error": exc.message,
        "hint": exc.hint,
    }
