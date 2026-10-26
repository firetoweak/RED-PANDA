from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from konden.assistant.artifacts import ArtifactGateway
from konden.assistant.context.projection import (
    ModelContextSettings,
    externalize_tool_result,
)
from konden.runtime import ToolBinding
from konden.runtime.dispatcher import AttemptContext
from konden.assistant.tool_results import runtime_tool_result
from konden.tools.spec import ToolArgumentsError
from konden.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE, SkillToolCatalog
from konden.assistant.catalog import CatalogSkill


class SkillToolAdapter:
    """Project enabled Skills as two ordinary Runtime tools."""

    def __init__(
        self,
        skills,
        gateway: ArtifactGateway,
        settings: ModelContextSettings,
        *,
        project_root: Path | None = None,
    ) -> None:
        self.skills = skills
        installed = skills.tool_catalog
        self._catalog = SkillToolCatalog(
            installed.registry,
            installed.package_reader,
            max_catalog_chars=installed.max_catalog_chars,
            project_root=project_root,
        )
        self._gateway = gateway
        self._settings = settings
        self._catalogs: dict[str, dict[str, int]] = {}

    def registry_catalog(self) -> list[dict[str, object]]:
        self._catalog.tool_specs()
        installed = [
            {"id": record.name, "description": record.description, "revision": record.revision}
            for record in self._catalog.registry.snapshot()
            if record.enabled
        ]
        project = [
            {"id": item.id, "description": item.description, "revision": item.revision}
            for item in self._catalog.project_skills()
        ]
        bundled = [
            {"id": item.id, "description": item.description, "revision": item.revision}
            for item in self._catalog.bundled_skills
        ]
        return sorted(installed + project + bundled, key=lambda item: item["id"])

    def apply_catalog(
        self,
        session_id: str,
        skills: tuple[CatalogSkill, ...],
    ) -> None:
        self._catalogs[session_id] = {
            item.id: item.revision for item in skills
        }

    def schemas(self) -> list[dict[str, object]]:
        return [spec.to_openai_tool() for spec in self._catalog.tool_specs({})]

    def bindings(self) -> dict[str, ToolBinding]:
        return {
            LOAD_SKILL: ToolBinding(self._handler(LOAD_SKILL)),
            READ_SKILL_RESOURCE: ToolBinding(self._handler(READ_SKILL_RESOURCE)),
        }

    def _handler(self, name: str):
        async def handler(
            context: AttemptContext,
            arguments: Mapping[str, object],
        ) -> object:
            catalog_specs = self._catalog.tool_specs(
                self._catalogs[context.session_id]
            )
            specs = {spec.name: spec for spec in catalog_specs}
            spec = specs.get(name)
            if spec is None:
                return {
                    "ok": False,
                    "code": "SKILL_NOT_AVAILABLE",
                    "error": "当前没有已启用的 Skill",
                    "hint": "先 load_management_tools 加载 skill，再用 list_installed_skills 查询或 propose_skill_set_enabled 启用。",
                }
            try:
                payload = spec.parameters.validate(dict(arguments))
            except ToolArgumentsError as exc:
                return {
                    "ok": False,
                    "code": "VALIDATION_ERROR",
                    "data": {"details": exc.details},
                    "error": "skill 参数校验失败",
                }
            result = runtime_tool_result(await spec.handler(payload))
            return externalize_tool_result(
                result,
                context.session_id,
                self._gateway,
                self._settings,
            )

        return handler
