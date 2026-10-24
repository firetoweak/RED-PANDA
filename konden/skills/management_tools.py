from __future__ import annotations

from pydantic import BaseModel, Field

from konden.tools.spec import PydanticParameters, ToolSpec
from konden.skills.application import SkillApplicationService
from konden.skills.errors import SkillInputError, SkillNotFoundError


LIST_INSTALLED_SKILLS = "list_installed_skills"
TEST_INSTALLED_SKILL = "test_installed_skill"


class SkillIdInput(BaseModel):
    skill_id: str = Field(description="已安装技能名称，可通过 list_installed_skills 查询。")


class SkillListInput(BaseModel):
    skill_id: str | None = Field(default=None, description="省略时列出全部（含停用项）；提供时只返回该 Skill。")


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
            "data": {"skills": [
                {key: value for key, value in record.to_dict().items() if key not in {"revision", "content_hash"}}
                for record in records
            ]},
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
                "enabled": result.record.enabled,
                "files": [{"path": path, "bytes": size} for path, size in result.files],
                "main_instruction_chars": result.main_instruction_chars,
            },
        }

    return (
        ToolSpec(
            LIST_INSTALLED_SKILLS,
            "列出已安装 Skill 的来源、版本和启用状态（含停用项），可按名称查询。"
            "不检查包是否完好，需要时用 test_installed_skill。",
            PydanticParameters(SkillListInput), list_skills,
        ),
        ToolSpec(
            TEST_INSTALLED_SKILL,
            "检查指定已安装包的身份、Frontmatter、路径、大小、登记信息与 hash 是否完好。"
            "只读，不运行脚本；通过只证明结构与完整性，不证明功能正确或内容安全。",
            PydanticParameters(SkillIdInput), test_skill,
        ),
    )


def _not_found(skill_id: str) -> dict:
    return {"ok": False, "code": "SKILL_NOT_INSTALLED", "data": {"skill_id": skill_id},
            "error": f"Skill 未安装: {skill_id}"}
