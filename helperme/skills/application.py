from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
import shutil
import tempfile

from helperme.paths import HelperMeHome
from helperme.skills.installer import LocalSkillInstaller
from helperme.skills.models import SkillBundle, SkillRecord, validate_skill_id
from helperme.skills.package import LocalSkillPackageReader
from helperme.skills.package import write_skill_bundle
from helperme.skills.registry import SkillRegistry
from helperme.skills.runtime import SkillToolCatalog
from helperme.skills.updates import SkillCandidateStore
from helperme.skills.models import SkillUpdateReport
from helperme.skills.models import SkillSourceRef
from helperme.skills.sources import SkillSourceRouter
from helperme.skills.summarizer import SkillDiffSummarizer
from helperme.skills.summarizer import InvalidSkillSummaryResponse
from helperme.llm.api import (
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)
from helperme.llm.types import InvalidLLMResponse
from helperme.skills.install_candidates import SkillInstallCandidateStore
from helperme.skills.models import SkillInstallCandidate
from helperme.skills.errors import (
    SkillAlreadyInstalledError,
    SkillInputError,
    SkillInstalledPackageError,
    SkillNotFoundError,
    SkillPreconditionError,
)
from helperme.skills.package import SkillPackageError


@dataclass(frozen=True)
class SkillTestResult:
    record: SkillRecord
    files: tuple[tuple[str, int], ...]
    main_instruction_chars: int


class SkillApplicationService:
    """Skill 用户控制面：安装、检查、启停与卸载。"""

    def __init__(
        self,
        home: HelperMeHome,
        registry: SkillRegistry | None = None,
        package_reader: LocalSkillPackageReader | None = None,
        *,
        max_catalog_chars: int = 20_000,
        source_router: SkillSourceRouter | None = None,
        diff_summarizer: SkillDiffSummarizer | None = None,
    ) -> None:
        if max_catalog_chars <= 0:
            raise ValueError("max_catalog_chars 必须大于 0")
        resolved_skills_root = home.skills_root.resolve()
        if registry is not None and registry.root != resolved_skills_root:
            raise ValueError("Skill Registry 必须属于当前 Skill storage root")
        self.skills_root = resolved_skills_root
        self.registry = (
            SkillRegistry(self.skills_root) if registry is None else registry
        )
        self.package_reader = (
            LocalSkillPackageReader()
            if package_reader is None
            else package_reader
        )
        self.installer = LocalSkillInstaller(
            self.skills_root,
            self.registry,
            self.package_reader,
        )
        self.tool_catalog = SkillToolCatalog(
            self.registry,
            self.package_reader,
            max_catalog_chars=max_catalog_chars,
        )
        self.max_catalog_chars = max_catalog_chars
        self.candidate_store = SkillCandidateStore(
            self.skills_root,
            self.package_reader,
        )
        self.source_router = (
            SkillSourceRouter(self.package_reader)
            if source_router is None
            else source_router
        )
        self.diff_summarizer = diff_summarizer
        self.install_candidates = SkillInstallCandidateStore(
            self.skills_root,
            self.package_reader,
        )
        self._management_lock = asyncio.Lock()

    async def list_skills(self) -> tuple[SkillRecord, ...]:
        return await self.registry.list_skills()

    async def install_local(self, source_directory: Path) -> SkillRecord:
        return await self.install_source(SkillSourceRef(
            "local",
            str(source_directory.resolve()),
        ))

    async def install_source(self, source: SkillSourceRef) -> SkillRecord:
        bundle = await self.source_router.fetch(source)
        async with self._management_lock:
            return await self._install_bundle(bundle)

    async def _check_catalog_budget(self, name: str, description: str) -> None:
        records = await self.registry.list_skills()
        entries = [
            (item.name, item.description) for item in records
            if item.enabled and item.name != name
        ]
        entries.append((name, description))
        catalog = "\n".join(f"- {key}: {value}" for key, value in entries)
        if len(catalog) > self.max_catalog_chars:
            raise SkillPreconditionError("SKILL_CATALOG_LIMIT: 启用后完整 Skill 目录超出预算")

    async def _install_bundle(self, bundle: SkillBundle) -> SkillRecord:
        await self._check_catalog_budget(bundle.name, bundle.description)
        return await self.installer.install_bundle(bundle)

    async def prepare_install(
        self,
        source: SkillSourceRef,
    ) -> SkillInstallCandidate:
        bundle = await self.source_router.fetch(source)
        async with self._management_lock:
            if await self.registry.get(bundle.name) is not None:
                raise SkillAlreadyInstalledError(
                    f"Skill 已安装: {bundle.name}"
                )
            await self._check_catalog_budget(bundle.name, bundle.description)
            return self.install_candidates.freeze(bundle)

    async def install_frozen(
        self,
        skill_id: str,
        content_hash: str,
        source: SkillSourceRef,
        resolved_ref: str,
    ) -> SkillRecord:
        bundle = self.install_candidates.load_bundle(
            skill_id,
            content_hash,
        )
        async with self._management_lock:
            return await self._install_bundle(replace(
                bundle,
                source=source,
                resolved_ref=resolved_ref,
            ))

    async def test_skill(self, skill_id: str) -> SkillTestResult:
        record, bundle = await self._validated_bundle(skill_id)
        return SkillTestResult(
            record=record,
            files=tuple((item.relative_path, item.size) for item in bundle.files),
            main_instruction_chars=len(bundle.main_instructions),
        )

    async def set_enabled(
        self,
        skill_id: str,
        enabled: bool,
    ) -> SkillRecord:
        async with self._management_lock:
            return await self._set_enabled_locked(skill_id, enabled)

    async def set_enabled_frozen(
        self,
        skill_id: str,
        expected_revision: int,
        expected_hash: str,
        enabled: bool,
    ) -> SkillRecord:
        async with self._management_lock:
            record = await self.registry.get(skill_id)
            if record is None:
                raise SkillNotFoundError(f"Skill 未安装: {skill_id}")
            if (
                record.revision != expected_revision
                or record.content_hash != expected_hash
            ):
                raise SkillPreconditionError(
                    f"Skill `{skill_id}` 在提交审批后已变化，请重新提交该操作"
                )
            return await self._set_enabled_locked(skill_id, enabled)

    async def _set_enabled_locked(
        self,
        skill_id: str,
        enabled: bool,
    ) -> SkillRecord:
        if await self.registry.get(skill_id) is None:
            raise SkillNotFoundError(f"Skill 未安装: {skill_id}")
        if enabled:
            record, _ = await self._validated_bundle(skill_id)
            await self._check_catalog_budget(record.name, record.description)
        return await self.registry.set_enabled(skill_id, enabled)

    async def check_update(
        self,
        skill_id: str,
        replacement_source: SkillSourceRef | None = None,
    ) -> SkillUpdateReport:
        async with self._management_lock:
            record, installed = await self._validated_bundle(skill_id)
            candidate_bundle = await self.source_router.fetch(
                replacement_source or record.source
            )
            candidate = self.candidate_store.freeze(
                record,
                installed,
                candidate_bundle,
            )
            if self.diff_summarizer is None:
                return SkillUpdateReport(
                    candidate,
                    summary_error="暂无语义概括，可按 Diff 决定是否批准",
                )
            try:
                summary = await self.diff_summarizer.summarize(
                    candidate,
                    installed.main_instructions,
                    candidate_bundle.main_instructions,
                )
            except (
                InvalidLLMResponse,
                InvalidSkillSummaryResponse,
                LLMContextLengthError,
                LLMProviderError,
                LLMTransientError,
            ) as exc:
                return SkillUpdateReport(
                    candidate,
                    summary_error=(
                        "Skill 更新概括生成失败: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            return SkillUpdateReport(candidate, semantic_summary=summary)

    async def update(
        self,
        skill_id: str,
        candidate_hash: str,
    ) -> SkillRecord:
        async with self._management_lock:
            current, _ = await self._validated_bundle(skill_id)
            candidate, bundle = self.candidate_store.load(
                skill_id,
                candidate_hash,
            )
            if (
                candidate.old_revision != current.revision
                or candidate.old_content_hash != current.content_hash
                or candidate.old_resolved_ref != current.resolved_ref
                or candidate.old_source != current.source
            ):
                raise SkillPreconditionError(
                    "Skill 在检查更新后已变化，请重新检查更新"
                )
            if not candidate.diff.changed:
                raise SkillPreconditionError("Skill 候选与当前安装内容相同")
            if current.enabled:
                await self._check_catalog_budget(bundle.name, bundle.description)
            return await self._replace_with_candidate(current, bundle)

    async def _replace_with_candidate(
        self,
        current: SkillRecord,
        bundle: SkillBundle,
    ) -> SkillRecord:
        target = self._package_directory(current.name)
        self.installer.staging_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(
            prefix=f"update-{current.name}-",
            dir=self.installer.staging_root,
        ))
        replacement = temporary / "replacement" / current.name
        backup = temporary / "backup" / current.name
        backup.parent.mkdir(parents=True)
        cleanup_temporary = True
        try:
            write_skill_bundle(replacement, bundle)
            self.package_reader.read(replacement)
            target.replace(backup)
            replacement.replace(target)
            proposed = SkillRecord(
                name=current.name,
                description=bundle.description,
                source=bundle.source,
                resolved_ref=bundle.resolved_ref,
                content_hash=bundle.content_hash,
                enabled=current.enabled,
                revision=current.revision,
                created_at=current.created_at,
            )
            try:
                return await self.registry.replace(proposed)
            except BaseException as registry_error:
                try:
                    if target.exists():
                        shutil.rmtree(target)
                    backup.replace(target)
                except BaseException as rollback_error:
                    cleanup_temporary = False
                    raise BaseExceptionGroup(
                        f"Skill Registry 更新失败且包回滚失败；备份保留在 {backup}",
                        [registry_error, rollback_error],
                    )
                raise
        finally:
            if cleanup_temporary and temporary.exists():
                shutil.rmtree(temporary)

    async def remove(
        self,
        skill_id: str,
        *,
        expected_revision: int | None = None,
        expected_hash: str | None = None,
    ) -> SkillRecord:
        try:
            validate_skill_id(skill_id)
        except ValueError as exc:
            raise SkillInputError(str(exc)) from exc
        async with self._management_lock:
            record = await self.registry.get(skill_id)
            if record is None:
                raise SkillNotFoundError(f"Skill 未安装: {skill_id}")
            if expected_revision is not None and (
                record.revision != expected_revision or record.content_hash != expected_hash
            ):
                raise SkillPreconditionError("Skill 在提交审批后已变化，请重新提交该操作")
            target = self._package_directory(skill_id)
            if not target.exists():
                return await self.registry.remove(skill_id)
            self.installer.staging_root.mkdir(parents=True, exist_ok=True)
            temporary_parent = Path(tempfile.mkdtemp(
                prefix=f"remove-{skill_id}-",
                dir=self.installer.staging_root,
            ))
            held_package = temporary_parent / skill_id
            target.replace(held_package)
            cleanup_temporary = True
            try:
                removed = await self.registry.remove(skill_id)
            except BaseException as registry_error:
                try:
                    held_package.replace(target)
                except BaseException as rollback_error:
                    cleanup_temporary = False
                    raise BaseExceptionGroup(
                        f"Skill Registry 删除失败且包回滚失败；备份保留在 {held_package}",
                        [registry_error, rollback_error],
                    )
                raise
            finally:
                if cleanup_temporary and temporary_parent.exists():
                    shutil.rmtree(temporary_parent)
            return removed

    async def _validated_bundle(
        self,
        skill_id: str,
    ) -> tuple[SkillRecord, SkillBundle]:
        try:
            validate_skill_id(skill_id)
        except ValueError as exc:
            raise SkillInputError(str(exc)) from exc
        record = await self.registry.get(skill_id)
        if record is None:
            raise SkillNotFoundError(f"Skill 未安装: {skill_id}")
        package_directory = self._package_directory(skill_id)
        if not package_directory.is_dir():
            raise SkillInstalledPackageError(
                f"已登记 Skill 包目录丢失: {skill_id}"
            )
        try:
            bundle = self.package_reader.read(package_directory)
        except (OSError, SkillPackageError) as exc:
            raise SkillInstalledPackageError(
                f"已安装 Skill 包无效: {skill_id}: {exc}"
            ) from exc
        if bundle.name != skill_id:
            raise SkillInstalledPackageError(
                f"Skill 严格身份不一致: directory={skill_id}, "
                f"frontmatter={bundle.name}"
            )
        if bundle.description != record.description:
            raise SkillInstalledPackageError(
                f"已安装 Skill 的登记描述与包内容不一致: {skill_id}"
            )
        if bundle.content_hash != record.content_hash:
            raise SkillInstalledPackageError(
                f"已安装 Skill 的登记 hash 与包内容不一致: {skill_id}"
            )
        return record, bundle

    def _package_directory(self, skill_id: str) -> Path:
        return (self.installer.packages_root / skill_id).resolve()
