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
from redpanda.skills.management_tools import create_skill_management_specs, create_skill_help_spec
from redpanda.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE, SkillToolCatalog
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
    listing, testing = create_skill_management_specs(service)
    # Schema source for the map. Execution stays on the resident tools, which
    # rebuild these specs against the Session catalog.
    read_specs = {item.name: item for item in service.tool_catalog.tool_specs()}
    operations = {
        "load": read_specs[LOAD_SKILL],
        "read_resource": read_specs[READ_SKILL_RESOURCE],
        "list": listing,
        "test": testing,
        **{name: item.proposal_spec for name, item in controls.items()},
    }
    return SkillAssembly(
        service=service,
        management_specs=(create_skill_help_spec(operations), listing, testing),
        control_operations=tuple(controls.values()),
    )
