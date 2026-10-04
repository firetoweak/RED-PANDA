from __future__ import annotations

from pathlib import Path
from typing import Literal, Mapping, cast

from pydantic import BaseModel, Field, model_validator

from redpanda.tools.control import (
    ControlApprovalExecution,
    ControlApprovalProposal,
    ControlPreparationFailure,
)
from redpanda.tools.spec import PydanticParameters, ToolSpec
from redpanda.skills.application import SkillApplicationService
from redpanda.skills.models import SkillSourceRef
from redpanda.skills.sources import SkillSourceError
from redpanda.skills.errors import (
    SkillAlreadyInstalledError,
    SkillInputError,
    SkillNotFoundError,
)


SKILL_INSTALL_ACTION = "skill.install"
SKILL_SET_ENABLED_ACTION = "skill.set_enabled"
SKILL_UNINSTALL_ACTION = "skill.uninstall"
SKILL_UPDATE_ACTION = "skill.update"
PROPOSE_SKILL_INSTALL = "propose_skill_install"
PROPOSE_SKILL_SET_ENABLED = "propose_skill_set_enabled"
PROPOSE_SKILL_UNINSTALL = "propose_skill_uninstall"
PROPOSE_SKILL_UPDATE = "propose_skill_update"


SOURCE_KIND_HELP = "local=本机目录；github=GitHub 仓库或子目录；url=HTTPS raw SKILL.md 或 ZIP。"
LOCATOR_HELP = (
    "local 必须是本机包含 SKILL.md 的绝对目录，不是文件，也不是相对工作区的路径；"
    "github 使用 owner/repo 或 https://github.com/owner/repo/tree/<ref>/<subpath>；"
    "url 使用 HTTPS raw Markdown 或 ZIP 地址，不接受普通 HTML 网页。"
    "仓库或 ZIP 未指定子目录时必须仅有一个 SKILL.md。工具自行下载，无需预先下载。"
)
REF_HELP = "仅 github 可用：分支、tag 或 commit；省略使用 HEAD。tree URL 已包含 ref 时不得再传。"


def _validate_source(source: SkillSourceRef) -> None:
    if source.kind == "local" and not Path(source.locator).is_absolute():
        raise ValueError("local locator 必须是本机绝对目录")
    if source.kind != "github" and source.requested_ref is not None:
        raise ValueError("requested_ref 仅适用于 github 来源")


class SkillInstallProposalInput(BaseModel):
    source_kind: Literal["local", "github", "url"] = Field(description=SOURCE_KIND_HELP)
    locator: str = Field(
        min_length=1,
        description=LOCATOR_HELP,
        examples=["https://github.com/typesafe-ai/skills/tree/main/skills/typesafe-ai"],
    )
    requested_ref: str | None = Field(default=None, description=REF_HELP)

    @model_validator(mode="after")
    def validate_source(self) -> "SkillInstallProposalInput":
        _validate_source(SkillSourceRef(
            self.source_kind, self.locator, self.requested_ref,
        ))
        return self


def create_skill_install_proposal_spec(
    service: SkillApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: SkillInstallProposalInput,
    ) -> ControlApprovalProposal | ControlPreparationFailure | dict:
        try:
            candidate = await service.prepare_install(SkillSourceRef(
                input_data.source_kind,
                input_data.locator,
                input_data.requested_ref,
            ))
        except SkillAlreadyInstalledError as exc:
            return {
                "ok": False,
                "code": "SKILL_ALREADY_INSTALLED",
                "data": {
                    "source_kind": input_data.source_kind,
                    "locator": input_data.locator,
                },
                "error": str(exc),
                "hint": "检查已安装目录；更新 Skill 应走独立更新流程。",
            }
        except SkillSourceError as exc:
            return ControlPreparationFailure({
                "ok": False,
                "code": "SKILL_SOURCE_ERROR",
                "data": {
                    "source_kind": input_data.source_kind,
                    "locator": input_data.locator,
                },
                "error": str(exc),
                "hint": "按来源格式和具体错误修正输入；不确定用法可查询 skill_help。",
            })
        except SkillInputError as exc:
            return _preparation_failure(exc)
        return ControlApprovalProposal(
            action=SKILL_INSTALL_ACTION,
            payload={
                "skill_id": candidate.skill_id,
                "content_hash": candidate.content_hash,
                "source": candidate.source.to_dict(),
                "resolved_ref": candidate.resolved_ref,
            },
            summary=(
                f"准备安装 Skill `{candidate.skill_id}`\n"
                f"描述：{candidate.description}\n"
                f"来源：{candidate.source.kind} {candidate.source.locator}\n"
                f"解析引用：{candidate.resolved_ref}\n"
                f"Content hash：{candidate.content_hash}\n"
                "批准后安装并启用；下一次决策进入能力目录。"
            ),
            risk=(
                "Skill 包可包含外部指令和脚本；"
                "当前只证明结构、路径、大小与 hash 符合契约，"
                "不证明内容安全。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_SKILL_INSTALL,
        description=(
            "从指定 local/GitHub/URL 来源安装 Skill。"
            "工具自行获取并校验，批准后安装并启用，无需先调用 test_installed_skill。"
            "只接受未安装的技能；已有技能用 propose_skill_update。本工具必须单独调用。"
        ),
        parameters=PydanticParameters(SkillInstallProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class SkillInstallApprovalHandler:
    action = SKILL_INSTALL_ACTION

    def __init__(self, service: SkillApplicationService) -> None:
        self.service = service

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        if set(payload) != {
            "skill_id",
            "content_hash",
            "source",
            "resolved_ref",
        }:
            raise SkillInputError("Skill install approval payload 字段不匹配")
        source = payload["source"]
        if not isinstance(source, Mapping):
            raise SkillInputError("Skill install approval source 必须是 object")
        skill_id = payload["skill_id"]
        content_hash = payload["content_hash"]
        resolved_ref = payload["resolved_ref"]
        if any(
            type(value) is not str
            for value in (skill_id, content_hash, resolved_ref)
        ):
            raise SkillInputError("Skill install approval identity 类型无效")
        try:
            record = await self.service.install_frozen(
                cast(str, skill_id),
                cast(str, content_hash),
                SkillSourceRef.from_dict(dict(source)),
                cast(str, resolved_ref),
            )
        except SkillInputError as exc:
            return ControlApprovalExecution(
                succeeded=False,
                message=f"Skill `{skill_id}` 安装未执行：{exc}",
            )
        return ControlApprovalExecution(
            succeeded=True,
            message=(
                f"Skill `{record.name}` 已安装并启用，包完整性校验通过。"
                "下一次决策进入能力目录；正文尚未加载，相关任务时按需 load_skill。"
            ),
            data={
                "skill_id": record.name,
                "revision": record.revision,
                "content_hash": record.content_hash,
                "enabled": record.enabled,
            },
        )


class SkillSetEnabledProposalInput(BaseModel):
    skill_id: str = Field(description="已安装技能的名称，可用 list_installed_skills 查询。")
    enabled: bool = Field(description="true 启用，false 停用；停用不删除已安装的包。")


def create_skill_set_enabled_proposal_spec(service: SkillApplicationService) -> ToolSpec:
    async def propose(input_data: SkillSetEnabledProposalInput):
        record = await service.registry.get(input_data.skill_id)
        if record is None:
            return _preparation_failure(SkillNotFoundError(
                f"Skill 未安装: {input_data.skill_id}"
            ))
        if record.enabled == input_data.enabled:
            return {
                "ok": True,
                "code": "SKILL_STATE_UNCHANGED",
                "data": record.to_dict(),
            }
        if input_data.enabled:
            try:
                await service.test_skill(record.name)
            except SkillInputError as exc:
                return _preparation_failure(exc)
        state = "启用" if input_data.enabled else "停用"
        return ControlApprovalProposal(
            action=SKILL_SET_ENABLED_ACTION,
            payload={
                "skill_id": record.name,
                "expected_revision": record.revision,
                "expected_hash": record.content_hash,
                "enabled": input_data.enabled,
            },
            summary=f"准备{state} Skill `{record.name}`，revision={record.revision}。",
            risk="批准后改变目录可用状态，下一次决策生效；不执行技能正文或脚本。",
        )

    return ToolSpec(
        PROPOSE_SKILL_SET_ENABLED,
        "启用或停用已安装 Skill，提交审批；状态相同时直接返回。无需先调用 test_installed_skill。"
        "启用允许后续按需加载，停用保留完整包。本工具必须单独调用。",
        PydanticParameters(SkillSetEnabledProposalInput),
        propose,
        control_boundary=True,
        exclusive_batch=True,
    )


def _frozen_identity(payload: Mapping[str, object]) -> tuple[str, int, str]:
    skill_id = payload["skill_id"]
    revision = payload["expected_revision"]
    content_hash = payload["expected_hash"]
    if (
        type(skill_id) is not str
        or type(revision) is not int
        or type(content_hash) is not str
    ):
        raise SkillInputError("Skill approval identity 类型无效")
    return skill_id, revision, content_hash


def _preparation_failure(exc: SkillInputError) -> ControlPreparationFailure:
    return ControlPreparationFailure({
        "ok": False,
        "code": "SKILL_PRECONDITION_FAILED",
        "data": {},
        "error": str(exc),
    })


class SkillSetEnabledApprovalHandler:
    action = SKILL_SET_ENABLED_ACTION

    def __init__(self, service: SkillApplicationService) -> None:
        self.service = service

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        if set(payload) != {"skill_id", "expected_revision", "expected_hash", "enabled"}:
            raise SkillInputError("Skill set_enabled approval payload 字段不匹配")
        skill_id, revision, content_hash = _frozen_identity(payload)
        enabled = payload["enabled"]
        if type(enabled) is not bool:
            raise SkillInputError("Skill enabled 必须是 bool")
        try:
            record = await self.service.set_enabled_frozen(
                skill_id, revision, content_hash, enabled,
            )
        except SkillInputError as exc:
            return ControlApprovalExecution(False, str(exc))
        state = "启用" if record.enabled else "停用"
        return ControlApprovalExecution(
            True, f"Skill `{record.name}` 已{state}，目录变化从下一次决策生效。",
            {
                "skill_id": record.name,
                "revision": record.revision,
                "enabled": record.enabled,
            },
        )


class SkillUninstallProposalInput(BaseModel):
    skill_id: str = Field(description="要移除的已安装技能名称；删除已安装的包及登记，不删除来源。")


def create_skill_uninstall_proposal_spec(service: SkillApplicationService) -> ToolSpec:
    async def propose(input_data: SkillUninstallProposalInput):
        record = await service.registry.get(input_data.skill_id)
        if record is None:
            return _preparation_failure(SkillNotFoundError(
                f"Skill 未安装: {input_data.skill_id}"
            ))
        return ControlApprovalProposal(
            action=SKILL_UNINSTALL_ACTION,
            payload={
                "skill_id": record.name,
                "expected_revision": record.revision,
                "expected_hash": record.content_hash,
            },
            summary=f"准备卸载 Skill `{record.name}`，revision={record.revision}。",
            risk="批准后删除已安装的包与登记；来源不变，下一次决策从目录移除。",
        )

    return ToolSpec(
        PROPOSE_SKILL_UNINSTALL,
        "卸载指定 Skill 的完整安装包与登记，提交审批；不删除原始来源。"
        "包损坏或丢失时仍可卸载登记。本工具必须单独调用。",
        PydanticParameters(SkillUninstallProposalInput),
        propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class SkillUninstallApprovalHandler:
    action = SKILL_UNINSTALL_ACTION

    def __init__(self, service: SkillApplicationService) -> None:
        self.service = service

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        if set(payload) != {"skill_id", "expected_revision", "expected_hash"}:
            raise SkillInputError("Skill uninstall approval payload 字段不匹配")
        skill_id, revision, content_hash = _frozen_identity(payload)
        try:
            record = await self.service.remove(
                skill_id, expected_revision=revision, expected_hash=content_hash,
            )
        except SkillInputError as exc:
            return ControlApprovalExecution(False, str(exc))
        return ControlApprovalExecution(
            True, f"Skill `{record.name}` 已卸载，下一次决策从能力目录移除。",
            {"skill_id": record.name},
        )


class SkillUpdateProposalInput(BaseModel):
    skill_id: str = Field(description="要更新的已安装技能名称。")
    source_kind: Literal["local", "github", "url"] | None = Field(
        default=None, description=SOURCE_KIND_HELP + " 省略沿用登记来源。",
    )
    locator: str | None = Field(default=None, description=LOCATOR_HELP)
    requested_ref: str | None = Field(default=None, description=REF_HELP)

    @model_validator(mode="after")
    def validate_replacement(self) -> "SkillUpdateProposalInput":
        if (self.source_kind is None) != (self.locator is None):
            raise ValueError("replacement source_kind/locator 必须同时提供")
        if self.source_kind is None and self.requested_ref is not None:
            raise ValueError("requested_ref 需要 replacement source")
        if self.source_kind is not None:
            _validate_source(SkillSourceRef(
                self.source_kind, cast(str, self.locator), self.requested_ref,
            ))
        return self

    def replacement(self) -> SkillSourceRef | None:
        if self.source_kind is None:
            return None
        return SkillSourceRef(
            self.source_kind,
            cast(str, self.locator),
            self.requested_ref,
        )


def create_skill_update_proposal_spec(
    service: SkillApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: SkillUpdateProposalInput,
    ) -> ControlApprovalProposal | ControlPreparationFailure | dict:
        try:
            report = await service.check_update(
                input_data.skill_id,
                input_data.replacement(),
            )
        except (SkillInputError, SkillSourceError) as exc:
            return ControlPreparationFailure({
                "ok": False,
                "code": "SKILL_UPDATE_CHECK_FAILED",
                "data": {"skill_id": input_data.skill_id},
                "error": str(exc),
                "hint": "按 error 核对 skill_id 与来源；包已损坏时不能更新，可卸载后重新安装。",
            })
        candidate = report.candidate
        if not candidate.diff.changed:
            return {
                "ok": True,
                "code": "SKILL_ALREADY_CURRENT",
                "data": report.to_dict(),
                "error": None,
                "hint": None,
            }
        return ControlApprovalProposal(
            action=SKILL_UPDATE_ACTION,
            payload={
                "skill_id": candidate.skill_id,
                "candidate_hash": candidate.candidate_hash,
            },
            summary=(
                f"准备{candidate.operation} Skill `{candidate.skill_id}`\n"
                f"Candidate hash：{candidate.candidate_hash}\n"
                f"Diff：{candidate.diff.to_dict()}\n"
                f"语义概括：{report.semantic_summary or report.summary_error}"
            ),
            risk=(
                "批准后用本次检查到的版本替换已安装 Skill，启用状态保持不变；"
                "若检查之后该 Skill 又有变化，更新会失败，需要重新检查。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_SKILL_UPDATE,
        description=(
            "为健康的已安装 Skill 检查来源更新，有变化时提交更新审批。"
            "省略来源时使用登记来源；保持启用状态。包损坏时不能更新，可卸载后重新安装。"
            "本工具必须单独调用。"
        ),
        parameters=PydanticParameters(SkillUpdateProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class SkillUpdateApprovalHandler:
    action = SKILL_UPDATE_ACTION

    def __init__(self, service: SkillApplicationService) -> None:
        self.service = service

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        if set(payload) != {"skill_id", "candidate_hash"}:
            raise SkillInputError("Skill update approval payload 字段不匹配")
        skill_id = payload["skill_id"]
        candidate_hash = payload["candidate_hash"]
        if type(skill_id) is not str or type(candidate_hash) is not str:
            raise SkillInputError("Skill update approval payload 类型无效")
        try:
            record = await self.service.update(skill_id, candidate_hash)
        except SkillInputError as exc:
            return ControlApprovalExecution(
                False,
                f"Skill `{skill_id}` 更新未执行：{exc}",
            )
        return ControlApprovalExecution(
            True,
            f"Skill `{record.name}` 已更新 (revision={record.revision})。",
            {"skill_id": record.name, "revision": record.revision},
        )
