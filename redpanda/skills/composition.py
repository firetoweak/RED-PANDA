from __future__ import annotations

from dataclasses import dataclass

from redpanda.paths import RedPandaHome
from redpanda.skills.application import SkillApplicationService
from redpanda.skills.summarizer import SkillDiffSummarizer
from redpanda.skills.approval import (
    SkillSetEnabledApprovalHandler,
    SkillInstallApprovalHandler,
    SkillUninstallApprovalHandler,
    SkillUpdateApprovalHandler,
    create_skill_set_enabled_proposal_spec,
    create_skill_install_proposal_spec,
    create_skill_uninstall_proposal_spec,
    create_skill_update_proposal_spec,
)
from redpanda.skills.management_tools import create_skill_management_specs
from redpanda.skills.runtime import SkillToolCatalog
from redpanda.tools.control import ControlOperation
from redpanda.tools.spec import ToolSpec


@dataclass(frozen=True)
class SkillAssembly:
    service: SkillApplicationService
    management_specs: tuple[ToolSpec, ...]
    control_operations: tuple[ControlOperation, ...]

    @property
    def tool_catalog(self) -> SkillToolCatalog:
        return self.service.tool_catalog


def build_skills(
    home: RedPandaHome,
    *,
    diff_summarizer: SkillDiffSummarizer | None = None,
) -> SkillAssembly:
    service = SkillApplicationService(
        home,
        diff_summarizer=diff_summarizer,
    )
    controls = {
        "install": ControlOperation(
            "skill", create_skill_install_proposal_spec(service),
            SkillInstallApprovalHandler(service),
        ),
        "update": ControlOperation(
            "skill", create_skill_update_proposal_spec(service),
            SkillUpdateApprovalHandler(service),
        ),
        "set_enabled": ControlOperation(
            "skill", create_skill_set_enabled_proposal_spec(service),
            SkillSetEnabledApprovalHandler(service),
        ),
        "uninstall": ControlOperation(
            "skill", create_skill_uninstall_proposal_spec(service),
            SkillUninstallApprovalHandler(service),
        ),
    }
    return SkillAssembly(
        service=service,
        management_specs=create_skill_management_specs(service),
        control_operations=tuple(controls.values()),
    )
