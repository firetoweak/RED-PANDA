from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from redpanda.tools.spec import JsonSchemaParameters, PydanticParameters, ToolSpec
from redpanda.skills.application import SkillApplicationService
from redpanda.skills.errors import SkillInputError, SkillNotFoundError


LIST_INSTALLED_SKILLS = "list_installed_skills"
TEST_INSTALLED_SKILL = "test_installed_skill"
SKILL_HELP = "skill_help"
SKILL_MANAGEMENT_GUIDE = (
    "已安装的 Skill 是独立副本，改动来源后需用 propose_skill_update 同步；"
    "可从任意来源安装，不限于已安装列表。"
)


class SkillIdInput(BaseModel):
    skill_id: str = Field(description="已安装技能名称，可通过 list_installed_skills 查询。")


class SkillListInput(BaseModel):
    skill_id: str | None = Field(default=None, description="省略列出全部；提供名称时精确查询该技能，包含 disabled 项。")


def create_skill_management_specs(service: SkillApplicationService) -> tuple[ToolSpec, ...]:
    async def list_skills(input_data: SkillListInput):
        if input_data.skill_id is None:
            records = await service.list_skills()
        else:
            record = await service.registry.get(input_data.skill_id)
            if record is None:
                return _not_found(input_data.skill_id)
            records = (record,)
        return {
            "ok": True, "code": "SKILLS_LISTED",
            "data": {"skills": [record.to_dict() for record in records]},
        }

    async def test_skill(input_data: SkillIdInput):
        try:
            result = await service.test_skill(input_data.skill_id)
        except SkillNotFoundError:
            return _not_found(input_data.skill_id)
        except SkillInputError as exc:
            return {"ok": False, "code": "SKILL_INVALID",
                    "data": {"skill_id": input_data.skill_id}, "error": str(exc)}
        return {
            "ok": True, "code": "SKILL_PACKAGE_VALID",
            "data": {
                "skill_id": result.record.name,
                "revision": result.record.revision,
                "content_hash": result.record.content_hash,
                "enabled": result.record.enabled,
                "checks": ["identity", "frontmatter", "paths", "size_limits", "registry_metadata", "content_hash"],
                "files": [{"path": path, "bytes": size} for path, size in result.files],
                "main_instruction_chars": result.main_instruction_chars,
            },
        }

    return (
        ToolSpec(
            LIST_INSTALLED_SKILLS,
            "查询已安装 Skill 的来源、hash、revision 与启用状态，可按名称查询。"
            "只读登记，不校验磁盘包；需要检查实际安装时调用 test_installed_skill。",
            PydanticParameters(SkillListInput), list_skills,
        ),
        ToolSpec(
            TEST_INSTALLED_SKILL,
            "检查指定已安装包的身份、Frontmatter、路径、大小、登记信息与 hash 是否完好。"
            "只读，不运行脚本；通过只证明结构与完整性，不证明功能正确或内容安全。",
            PydanticParameters(SkillIdInput), test_skill,
        ),
    )


def create_skill_help_spec(operations: Mapping[str, ToolSpec]) -> ToolSpec:
    # One operation table owns both navigation names and the real callable schemas.
    parameters = JsonSchemaParameters({
        "type": "object",
        "properties": {"operation": {
            "type": "string", "enum": ["help", *operations],
            "description": "省略返回管理地图；指定操作返回其真实参数契约和示例。",
        }},
        "additionalProperties": False,
    })

    async def help_skill(input_data: dict):
        operation = input_data.get("operation")
        all_operations = {"help": spec, **operations}
        if operation is None:
            return {
                "ok": True, "code": "SKILL_HELP",
                "data": {"operations": [
                    {"operation": key, "tool": item.name, "description": item.description,
                     "requires_approval": item.control_boundary}
                    for key, item in all_operations.items()
                ]},
            }
        selected = all_operations[operation]
        return {
            "ok": True, "code": "SKILL_HELP",
            "data": {"operation": operation,
                     "requires_approval": selected.control_boundary,
                     "tool": selected.to_openai_tool()["function"]},
        }

    spec = ToolSpec(
        SKILL_HELP, SKILL_MANAGEMENT_GUIDE + "本工具返回管理操作清单或某项操作的完整参数，不触发安装或审批。",
        parameters, help_skill,
    )
    return spec


def _not_found(skill_id: str) -> dict:
    return {"ok": False, "code": "SKILL_NOT_INSTALLED", "data": {"skill_id": skill_id},
            "error": f"Skill 未安装: {skill_id}"}
