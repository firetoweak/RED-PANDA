from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from dataclasses import replace

from redpanda.skills.models import (
    SkillBundle,
    SkillManifestDiff,
    SkillRecord,
    SkillUpdateCandidate,
)
from redpanda.skills.package import LocalSkillPackageReader, write_skill_bundle
from redpanda.skills.errors import (
    SkillCandidateNotFoundError,
    SkillInputError,
    SkillPreconditionError,
)


def manifest_diff(
    installed: SkillBundle,
    candidate: SkillBundle,
) -> SkillManifestDiff:
    old = {
        item.relative_path: hashlib.sha256(item.content).hexdigest()
        for item in installed.files
    }
    new = {
        item.relative_path: hashlib.sha256(item.content).hexdigest()
        for item in candidate.files
    }
    return SkillManifestDiff(
        added=tuple(sorted(new.keys() - old.keys())),
        modified=tuple(sorted(
            path for path in new.keys() & old.keys()
            if new[path] != old[path]
        )),
        deleted=tuple(sorted(old.keys() - new.keys())),
    )


class SkillCandidateStore:
    """持久保存内容寻址的更新候选及其冻结差异。"""

    def __init__(
        self,
        skills_root: Path,
        package_reader: LocalSkillPackageReader | None = None,
    ) -> None:
        self.skills_root = skills_root.resolve()
        self.root = self.skills_root / ".candidates" / "update"
        self.package_reader = (
            LocalSkillPackageReader()
            if package_reader is None
            else package_reader
        )

    def freeze(
        self,
        current: SkillRecord,
        installed: SkillBundle,
        bundle: SkillBundle,
    ) -> SkillUpdateCandidate:
        if bundle.name != current.name:
            raise SkillPreconditionError(
                f"同源 update 不允许改变 Skill 身份: "
                f"{current.name} -> {bundle.name}"
            )
        candidate = SkillUpdateCandidate(
            skill_id=current.name,
            old_revision=current.revision,
            old_resolved_ref=current.resolved_ref,
            old_content_hash=current.content_hash,
            old_source=current.source,
            new_resolved_ref=bundle.resolved_ref,
            candidate_hash=bundle.content_hash,
            source=bundle.source,
            operation=(
                "update" if bundle.source == current.source else "replace"
            ),
            diff=manifest_diff(installed, bundle),
        )
        destination = self._candidate_directory(candidate.candidate_hash)
        if destination.exists():
            frozen, frozen_bundle = self.load(
                candidate.skill_id,
                candidate.candidate_hash,
            )
            if (
                frozen.skill_id != candidate.skill_id
                or frozen.old_revision != candidate.old_revision
                or frozen.old_content_hash != candidate.old_content_hash
                or frozen.old_source != candidate.old_source
                or frozen.new_resolved_ref != candidate.new_resolved_ref
                or frozen.candidate_hash != candidate.candidate_hash
                or frozen.source != candidate.source
                or frozen.operation != candidate.operation
                or frozen.diff != candidate.diff
                or frozen_bundle.content_hash != bundle.content_hash
            ):
                raise RuntimeError("candidate hash 目录与冻结内容不一致")
            return frozen

        self.root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="candidate-", dir=self.root))
        try:
            package = temporary / "package" / bundle.name
            write_skill_bundle(package, bundle)
            self.package_reader.read(package)
            (temporary / "candidate.json").write_text(
                json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return candidate

    def load(
        self,
        skill_id: str,
        candidate_hash: str,
    ) -> tuple[SkillUpdateCandidate, SkillBundle]:
        directory = self._candidate_directory(candidate_hash)
        metadata_path = directory / "candidate.json"
        if not metadata_path.is_file():
            raise SkillCandidateNotFoundError(
                f"Skill update candidate 不存在: {candidate_hash}"
            )
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        candidate = SkillUpdateCandidate.from_dict(payload)
        if candidate.skill_id != skill_id or candidate.candidate_hash != candidate_hash:
            raise RuntimeError("Skill candidate 身份或 hash 索引不一致")
        bundle = self.package_reader.read(
            directory / "package" / candidate.skill_id
        )
        if bundle.content_hash != candidate.candidate_hash:
            raise RuntimeError("Skill candidate 冻结包 hash 已变化")
        return candidate, replace(
            bundle,
            source=candidate.source,
            resolved_ref=candidate.new_resolved_ref,
        )

    def _candidate_directory(self, candidate_hash: str) -> Path:
        if (
            len(candidate_hash) != 64
            or any(character not in "0123456789abcdef" for character in candidate_hash)
        ):
            raise SkillInputError("candidate_hash 必须是 64 位小写 SHA-256")
        return self.root / candidate_hash
