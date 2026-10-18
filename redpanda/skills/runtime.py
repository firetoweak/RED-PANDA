from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from redpanda.tools.spec import PydanticParameters, ToolSpec
from redpanda.skills.models import SkillBundle, SkillRecord
from redpanda.skills.package import (
    LocalSkillPackageReader,
    SkillPackageError,
    validate_relative_skill_path,
)
from redpanda.skills.registry import SkillRegistry


LOAD_SKILL = "load_skill"
READ_SKILL_RESOURCE = "read_skill_resource"
PROJECT_SKILL_PREFIX = "project:"
PROJECT_SKILLS_DIRECTORY = (".claude", "skills")
PROJECT_SKILL_OVER_BUDGET = "项目 Skill 无法使用：技能目录超出长度上限，精简项目 Skill 的描述或数量后可用。"


@dataclass(frozen=True, slots=True)
class ProjectSkill:
    """A Skill package read from the session's task root, not installed."""

    id: str
    description: str
    revision: int
    directory: Path
    bundle: SkillBundle | None


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
    offset: int = Field(default=0, ge=0, description="起始字符位置；续读时填上一次结果的 next_offset。")
    limit: int = Field(default=20_000, ge=1, le=50_000, description="本次最多读取的字符数。")


class SkillToolCatalog:
    """把已启用 Skill 投影为两个普通工具。"""

    def __init__(
        self,
        registry: SkillRegistry,
        package_reader: LocalSkillPackageReader | None = None,
        *,
        max_catalog_chars: int = 20_000,
        project_root: Path | None = None,
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
        self.project_root = project_root

    def project_skills(self) -> tuple[ProjectSkill, ...]:
        """Packages under the task root's .claude/skills, read as they are on disk now.

        Repository content must never stop the Agent: a package that cannot be
        read, a skills directory that leads outside the task root, or packages
        beyond the catalog budget stay listed with the reason instead.
        """
        if self.project_root is None:
            return ()
        directory = self.project_root.joinpath(*PROJECT_SKILLS_DIRECTORY)
        if not directory.is_dir():
            return ()
        if not directory.resolve().is_relative_to(self.project_root.resolve()):
            return (ProjectSkill(
                PROJECT_SKILL_PREFIX + "/".join(PROJECT_SKILLS_DIRECTORY),
                "项目 Skill 无法使用：这个目录指向任务根以外，不读取其中的 Skill。",
                1, directory.absolute(), None,
            ),)
        return self._within_budget(self._read_project_packages(directory))

    def _within_budget(self, skills: list[ProjectSkill]) -> tuple[ProjectSkill, ...]:
        installed = "\n".join(
            f"- {record.name}: {record.description}"
            for record in self.registry.snapshot()
            if record.enabled
        )
        used = len(installed)
        fitted = []
        for skill in skills:
            for candidate in (skill, ProjectSkill(skill.id, PROJECT_SKILL_OVER_BUDGET, 1, skill.directory, None)):
                cost = len(f"- {candidate.id}: {candidate.description}") + (1 if used else 0)
                if used + cost <= self.max_catalog_chars:
                    fitted.append(candidate)
                    used += cost
                    break
        return tuple(fitted)

    def _read_project_packages(self, directory: Path) -> list[ProjectSkill]:
        skills = []
        for package in sorted(item for item in directory.iterdir() if item.is_dir()):
            skill_id = PROJECT_SKILL_PREFIX + package.name
            try:
                bundle = self.package_reader.read(package)
                if bundle.name != package.name:
                    raise SkillPackageError(
                        f"SKILL.md 中的 name {bundle.name} 与目录名 {package.name} 不一致"
                    )
            except SkillPackageError as exc:
                skills.append(ProjectSkill(skill_id, f"项目 Skill 无法使用：{exc}", 1, package.absolute(), None))
                continue
            skills.append(ProjectSkill(
                skill_id, bundle.description, _content_revision(bundle.content_hash), package.resolve(), bundle,
            ))
        return skills

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
        project = self.project_skills()
        rendered_catalog = "\n".join(
            [f"- {record.name}: {record.description}" for record in records]
            + [f"- {item.id}: {item.description}" for item in project]
        )
        if len(rendered_catalog) > self.max_catalog_chars:
            raise RuntimeError("SKILL_CATALOG_LIMIT: 完整 Skill 目录超出预算")
        revisions = (
            {
                **{record.name: record.revision for record in records},
                **{item.id: item.revision for item in project},
            }
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
                if input_data.skill_id.startswith(PROJECT_SKILL_PREFIX):
                    project = self._require_current_project(input_data.skill_id, revision)
                    package_directory, bundle = project.directory, project.bundle
                else:
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
                    "skill_id": input_data.skill_id,
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
                if input_data.skill_id.startswith(PROJECT_SKILL_PREFIX):
                    package_directory = self._require_current_project(
                        input_data.skill_id, revision,
                    ).directory
                else:
                    current = await self._require_current_record(
                        input_data.skill_id,
                        revision,
                    )
                    package_directory, _ = self._validated_bundle(current)
                return self._read_resource(
                    input_data.skill_id,
                    package_directory,
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
                    "结果中的 skill_dir 是 Skill 包所在目录的绝对路径，供命令中引用包内脚本。"
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
                f"Skill {skill_id} 已被修改或停用",
                hint="按上下文中的当前技能目录重新选择。",
                data={"skill_id": skill_id},
            )
        return current

    def _require_current_project(self, skill_id: str, revision: int) -> ProjectSkill:
        current = next((item for item in self.project_skills() if item.id == skill_id), None)
        if current is None or current.revision != revision:
            raise SkillRuntimeError(
                "SKILL_CATALOG_STALE",
                f"项目 Skill {skill_id} 已在工作树中被修改或删除",
                hint="按上下文中的当前技能目录重新选择。",
                data={"skill_id": skill_id},
            )
        if current.bundle is None:
            raise SkillRuntimeError(
                "SKILL_PACKAGE_INVALID",
                current.description,
                hint="修正工作树中这个 Skill 包后，按更新后的技能目录重新加载。",
                data={"skill_id": skill_id},
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
        skill_id: str,
        package_directory: Path,
        relative_path: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        try:
            normalized = validate_relative_skill_path(relative_path)
        except SkillPackageError as exc:
            raise SkillRuntimeError(
                "INVALID_SKILL_RESOURCE_PATH",
                str(exc),
                hint="使用该 Skill 包内的相对路径，参考 load_skill 返回的正文。",
                data={"skill_id": skill_id, "relative_path": relative_path},
            ) from exc
        resource = package_directory.joinpath(*PurePosixPath(normalized).parts)
        if not resource.is_file():
            raise SkillRuntimeError(
                "SKILL_RESOURCE_NOT_FOUND",
                f"Skill 资源不存在: {relative_path}",
                hint="根据 SKILL.md 中列出的 supporting file 路径重试。",
                data={"skill_id": skill_id, "relative_path": relative_path},
            )
        try:
            content = resource.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillRuntimeError(
                "SKILL_RESOURCE_NOT_TEXT",
                f"Skill 资源不是 UTF-8 文本: {relative_path}",
                hint="该资源不是文本，无法读取。",
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
                "skill_id": skill_id,
                "relative_path": normalized,
                "content": content[offset:end],
                "offset": offset,
                "next_offset": end if end < len(content) else None,
                "total_chars": len(content),
            },
        }


def _content_revision(content_hash: str) -> int:
    """Catalog revisions are positive integers; a content hash fixes one per package state."""
    return int(content_hash.removeprefix("sha256:")[:12], 16) + 1


def _error_result(exc: SkillRuntimeError) -> dict[str, Any]:
    return {
        "ok": False,
        "code": exc.code,
        "data": exc.data or None,
        "error": exc.message,
        "hint": exc.hint,
    }
