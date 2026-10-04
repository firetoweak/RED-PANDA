from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
from typing import Protocol

from redpanda.skills.models import SkillBundle, SkillRecord
from redpanda.skills.errors import SkillAlreadyInstalledError
from redpanda.skills.package import (
    LocalSkillPackageReader,
    write_skill_bundle,
)


class SkillRegistryWriter(Protocol):
    async def get(self, skill_id: str) -> SkillRecord | None:
        ...

    async def add(self, record: SkillRecord) -> SkillRecord:
        ...


class LocalSkillInstaller:
    """本地目录安装：冻结包、staging 校验、原子发布、提交 Registry。"""

    def __init__(
        self,
        skills_root: Path,
        registry: SkillRegistryWriter,
        package_reader: LocalSkillPackageReader | None = None,
    ) -> None:
        self.skills_root = skills_root.resolve()
        self.packages_root = self.skills_root / "packages"
        self.staging_root = self.skills_root / ".staging"
        self.registry = registry
        self.package_reader = (
            LocalSkillPackageReader()
            if package_reader is None
            else package_reader
        )
        self._lock = asyncio.Lock()

    async def install(self, source_directory: Path) -> SkillRecord:
        bundle = self.package_reader.read(source_directory)
        return await self.install_bundle(bundle)

    async def install_bundle(self, bundle: SkillBundle) -> SkillRecord:
        async with self._lock:
            existing = await self.registry.get(bundle.name)
            if existing is not None:
                raise SkillAlreadyInstalledError(f"Skill 已安装: {bundle.name}")
            target = self._target(bundle.name)
            if target.exists():
                raise RuntimeError(
                    f"Skill 安装目录已存在但未登记: {target}"
                )

            self.packages_root.mkdir(parents=True, exist_ok=True)
            self.staging_root.mkdir(parents=True, exist_ok=True)
            staging_parent = Path(tempfile.mkdtemp(
                prefix=f"{bundle.name}-",
                dir=self.staging_root,
            ))
            staging_package = staging_parent / bundle.name
            try:
                write_skill_bundle(staging_package, bundle)
                self.package_reader.read(staging_package)
                os.replace(staging_package, target)
                record = SkillRecord(
                    name=bundle.name,
                    description=bundle.description,
                    source=bundle.source,
                    resolved_ref=bundle.resolved_ref,
                    content_hash=bundle.content_hash,
                    enabled=True,
                )
                try:
                    return await self.registry.add(record)
                except BaseException as registry_error:
                    try:
                        shutil.rmtree(target)
                    except BaseException as cleanup_error:
                        raise BaseExceptionGroup(
                            "Skill Registry 写入失败且安装目录清理失败",
                            [registry_error, cleanup_error],
                        )
                    raise
            finally:
                if staging_parent.exists():
                    shutil.rmtree(staging_parent)

    def _target(self, skill_id: str) -> Path:
        return (self.packages_root / skill_id).resolve()
