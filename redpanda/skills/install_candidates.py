from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

from redpanda.skills.errors import SkillCandidateNotFoundError
from redpanda.skills.models import SkillBundle, SkillInstallCandidate
from redpanda.skills.package import LocalSkillPackageReader, write_skill_bundle


class SkillInstallCandidateStore:
    """持久保存内容寻址的安装候选；来源身份不参与包内容去重。"""

    def __init__(
        self,
        skills_root: Path,
        package_reader: LocalSkillPackageReader,
    ) -> None:
        self.skills_root = skills_root.resolve()
        self.root = self.skills_root / ".candidates" / "install"
        self.package_reader = package_reader

    def freeze(self, bundle: SkillBundle) -> SkillInstallCandidate:
        candidate = SkillInstallCandidate(
            skill_id=bundle.name,
            description=bundle.description,
            source=bundle.source,
            resolved_ref=bundle.resolved_ref,
            content_hash=bundle.content_hash,
        )
        destination = self._directory(candidate.content_hash)
        if destination.exists():
            frozen_bundle = self.load_bundle(
                candidate.skill_id,
                candidate.content_hash,
            )
            if (
                frozen_bundle.name != candidate.skill_id
                or frozen_bundle.description != candidate.description
                or frozen_bundle.content_hash != bundle.content_hash
            ):
                raise RuntimeError("install candidate hash 与已冻结内容冲突")
            return candidate

        self.root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(
            prefix="install-candidate-",
            dir=self.root,
        ))
        try:
            package = temporary / "package" / bundle.name
            write_skill_bundle(package, bundle)
            self.package_reader.read(package)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return candidate

    def load_bundle(
        self,
        skill_id: str,
        content_hash: str,
    ) -> SkillBundle:
        directory = self._directory(content_hash)
        if not directory.is_dir():
            raise SkillCandidateNotFoundError(
                f"Skill install candidate 不存在: {content_hash}"
            )
        bundle = self.package_reader.read(
            directory / "package" / skill_id
        )
        if bundle.content_hash != content_hash:
            raise RuntimeError("install candidate 冻结包 hash 已变化")
        return bundle

    def _directory(self, content_hash: str) -> Path:
        if (
            len(content_hash) != 64
            or any(character not in "0123456789abcdef" for character in content_hash)
        ):
            raise ValueError("content_hash 必须是 64 位小写 SHA-256")
        return self.root / content_hash
