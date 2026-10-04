"""Skill 安装、管理与渐进加载能力。"""

from redpanda.skills.models import (
    SkillBundle,
    SkillFile,
    SkillPackageLimits,
    SkillRecord,
    SkillSourceRef,
)
from redpanda.skills.installer import LocalSkillInstaller
from redpanda.skills.application import SkillApplicationService, SkillTestResult
from redpanda.skills.console import SkillCommandError, SkillConsoleAdapter
from redpanda.skills.package import LocalSkillPackageReader, SkillPackageError
from redpanda.skills.registry import SkillRegistry
from redpanda.skills.runtime import (
    LOAD_SKILL,
    READ_SKILL_RESOURCE,
    SkillToolCatalog,
    SkillRuntimeError,
)
from redpanda.skills.sources import SkillSourceError, SkillSourceRouter
from redpanda.skills.summarizer import LlmSkillDiffSummarizer, SkillDiffSummarizer
from redpanda.skills.approval import (
    PROPOSE_SKILL_SET_ENABLED,
    PROPOSE_SKILL_INSTALL,
    PROPOSE_SKILL_UNINSTALL,
    PROPOSE_SKILL_UPDATE,
    SkillSetEnabledApprovalHandler,
    SkillInstallApprovalHandler,
    SkillUninstallApprovalHandler,
    SkillUpdateApprovalHandler,
    create_skill_install_proposal_spec,
    create_skill_uninstall_proposal_spec,
    create_skill_update_proposal_spec,
)

__all__ = [
    "LocalSkillPackageReader",
    "LOAD_SKILL",
    "READ_SKILL_RESOURCE",
    "SkillToolCatalog",
    "SkillRuntimeError",
    "LocalSkillInstaller",
    "SkillApplicationService",
    "SkillBundle",
    "SkillFile",
    "SkillTestResult",
    "SkillCommandError",
    "SkillConsoleAdapter",
    "SkillPackageError",
    "SkillPackageLimits",
    "SkillRecord",
    "SkillRegistry",
    "SkillSourceRef",
    "SkillSourceError",
    "SkillSourceRouter",
    "SkillDiffSummarizer",
    "LlmSkillDiffSummarizer",
    "PROPOSE_SKILL_INSTALL",
    "PROPOSE_SKILL_SET_ENABLED",
    "PROPOSE_SKILL_UPDATE",
    "PROPOSE_SKILL_UNINSTALL",
    "SkillSetEnabledApprovalHandler",
    "SkillInstallApprovalHandler",
    "SkillUpdateApprovalHandler",
    "SkillUninstallApprovalHandler",
    "create_skill_install_proposal_spec",
    "create_skill_update_proposal_spec",
    "create_skill_uninstall_proposal_spec",
]
