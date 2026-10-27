from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from konden.cli.errors import (
    CliAlreadyInstalledError,
    CliInputError,
    CliNotFoundError,
)
from konden.cli.health import CliHealthChecker, CliProbe
from konden.cli.installer import resolve_cli_path
from konden.cli.models import (
    CliRecord,
    CliSourceRef,
    validate_cli_description,
    validate_cli_id,
)
from konden.cli.provision import provision_github_release, resolve_release_tag
from konden.cli.registry import CliRegistry
from konden.cli.runtime import CliToolCatalog
from konden.paths import KondenHome
from konden.execution.command import CommandExecutor


@dataclass(frozen=True)
class CliInstallCandidate:
    """propose 阶段冻结的登记身份；version/health 在执行时重新取最新事实。"""

    name: str
    description: str
    source: CliSourceRef
    resolved_path: str
    probed: CliProbe | None


@dataclass(frozen=True)
class CliTestResult:
    record: CliRecord
    probed: CliProbe


class CliApplicationService:
    """CLI 用户控制面：登记、检查、体检、刷新、修复与移除。"""

    def __init__(
        self,
        home: KondenHome,
        command_executor: CommandExecutor,
        registry: CliRegistry | None = None,
        *,
        max_catalog_chars: int = 20_000,
    ) -> None:
        if max_catalog_chars <= 0:
            raise ValueError("max_catalog_chars 必须大于 0")
        clis_root = home.clis_root.resolve()
        if registry is not None and registry.root != clis_root:
            raise ValueError("CLI Registry 必须属于当前 CLI storage root")
        self.clis_root = clis_root
        self.registry = CliRegistry(clis_root) if registry is None else registry
        self.health_checker = CliHealthChecker(command_executor, cwd=clis_root)
        self.tool_catalog = CliToolCatalog(
            self.registry,
            max_catalog_chars=max_catalog_chars,
        )
        self._management_lock = asyncio.Lock()

    async def list_clis(self) -> tuple[CliRecord, ...]:
        return await self.registry.list_clis()

    async def inspect(self, cli_id: str) -> CliRecord:
        record = await self.registry.get(cli_id)
        if record is None:
            raise CliNotFoundError(f"CLI 未登记: {cli_id}")
        return record

    async def inspect_or_none(self, cli_id: str) -> CliRecord | None:
        return await self.registry.get(cli_id)

    async def test_cli(self, cli_id: str) -> CliTestResult:
        """重跑体检。纯诊断：返回测得事实，不写回 Registry。"""
        record = await self.registry.get(cli_id)
        if record is None:
            raise CliNotFoundError(f"CLI 未登记: {cli_id}")
        return CliTestResult(record=record, probed=await self._probe(record))

    async def prepare_install(
        self,
        name: str,
        description: str,
        locator: str,
    ) -> CliInstallCandidate:
        validate_cli_id(name)
        validate_cli_description(description)
        async with self._management_lock:
            if await self.registry.get(name) is not None:
                raise CliAlreadyInstalledError(f"CLI 已登记: {name}")
        resolved_path = resolve_cli_path(locator)
        probed = await self.health_checker.probe(resolved_path)
        return CliInstallCandidate(
            name=name,
            description=description,
            source=CliSourceRef("manifest", locator),
            resolved_path=resolved_path,
            probed=probed,
        )

    async def prepare_release_install(
        self,
        name: str,
        description: str,
        repository: str,
        requested_version: str | None = None,
    ) -> CliInstallCandidate:
        """propose 阶段只解析并冻结确定的 tag，不下载归档。

        审批不保存 latest 这类移动目标：未钉版本时在这里解析成具体 tag，
        之后 install/repair 都按该 tag 取，避免批的是 A 装的是 B。
        """
        validate_cli_id(name)
        validate_cli_description(description)
        source = CliSourceRef("github-release", repository, requested_version)
        async with self._management_lock:
            if await self.registry.get(name) is not None:
                raise CliAlreadyInstalledError(f"CLI 已登记: {name}")
        resolved_source = CliSourceRef(
            "github-release",
            repository,
            await resolve_release_tag(source),
        )
        return CliInstallCandidate(
            name=name,
            description=description,
            source=resolved_source,
            resolved_path="",
            probed=None,
        )

    async def install_frozen(
        self,
        name: str,
        description: str,
        source: CliSourceRef,
        resolved_path: str,
    ) -> CliRecord:
        """按批准的身份登记；version/health 取执行时的最新系统事实。"""
        async with self._management_lock:
            if await self.registry.get(name) is not None:
                raise CliAlreadyInstalledError(f"CLI 已登记: {name}")
            if source.kind == "github-release":
                provisioned = await provision_github_release(
                    name,
                    source,
                    install_root=self.clis_root / "_installs",
                )
                resolved_path = provisioned.resolved_path
            elif not Path(resolved_path).is_file():
                raise CliInputError(
                    f"候选路径已失效: {resolved_path}。请重新 propose。"
                )
            probed = await self.health_checker.probe(resolved_path)
            return await self.registry.add(CliRecord(
                name=name,
                description=description,
                source=source,
                version=probed.version,
                resolved_path=resolved_path,
                health=probed.health,
            ))

    async def refresh(self, cli_id: str, *, expected_revision: int) -> CliRecord:
        """update 的 manifest 语义：用户自行升级后，重跑体检更新 version/health。

        不重新解析路径——路径漂移属于 repair。
        """
        async with self._management_lock:
            record = await self._require_at_revision(cli_id, expected_revision)
            if record.resolved_path is None or not Path(
                record.resolved_path
            ).is_file():
                raise CliInputError(
                    f"CLI {cli_id} 登记路径已失效，请改用 repair 重新解析。"
                )
            probed = await self.health_checker.probe(record.resolved_path)
            return await self.registry.replace(CliRecord(
                name=record.name,
                description=record.description,
                source=record.source,
                version=probed.version,
                resolved_path=record.resolved_path,
                health=probed.health,
                revision=record.revision,
                created_at=record.created_at,
            ))

    async def repair(self, cli_id: str, *, expected_revision: int) -> CliRecord:
        """重新确定 resolved_path 并重跑体检。

        manifest 源重新解析本机路径；github-release 源按冻结的 tag 重新下载解压，
        因为它的 resolved_path 指向 konden 自己管理的安装目录。
        """
        async with self._management_lock:
            record = await self._require_at_revision(cli_id, expected_revision)
            if record.source.kind == "github-release":
                provisioned = await provision_github_release(
                    record.name,
                    record.source,
                    install_root=self.clis_root / "_installs",
                )
                resolved_path = provisioned.resolved_path
            else:
                resolved_path = resolve_cli_path(record.source.locator)
            probed = await self.health_checker.probe(resolved_path)
            return await self.registry.replace(CliRecord(
                name=record.name,
                description=record.description,
                source=record.source,
                version=probed.version,
                resolved_path=resolved_path,
                health=probed.health,
                revision=record.revision,
                created_at=record.created_at,
            ))

    async def uninstall(self, cli_id: str, *, expected_revision: int) -> CliRecord:
        """移除登记。manifest 源不卸载软件本身——那是用户的手工安装；
        github-release 源连 konden 自己下载的安装目录一起清掉。"""
        async with self._management_lock:
            record = await self._require_at_revision(cli_id, expected_revision)
            removed = await self.registry.remove(cli_id)
            if record.source.kind == "github-release":
                self._remove_install_dir(cli_id)
            return removed

    def _remove_install_dir(self, cli_id: str) -> None:
        """只删 <clis_root>/_installs/<cli_id>，不碰该目录之外的任何东西。"""
        base = (self.clis_root / "_installs").resolve()
        target = (base / cli_id).resolve()
        if target != base and base in target.parents and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    async def _probe(self, record: CliRecord) -> CliProbe:
        if record.resolved_path is None or not Path(record.resolved_path).is_file():
            raise CliNotFoundError(
                f"CLI {record.name} 已登记但可执行文件不存在: "
                f"{record.resolved_path}"
            )
        return await self.health_checker.probe(record.resolved_path)

    async def _require_at_revision(
        self,
        cli_id: str,
        expected_revision: int,
    ) -> CliRecord:
        record = await self.registry.get(cli_id)
        if record is None:
            raise CliNotFoundError(f"CLI 未登记: {cli_id}")
        if record.revision != expected_revision:
            raise CliInputError(
                f"CLI `{cli_id}` 在提交后已被修改"
            )
        return record
