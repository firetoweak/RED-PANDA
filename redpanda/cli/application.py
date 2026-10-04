from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from redpanda.cli.errors import (
    CliAlreadyInstalledError,
    CliInputError,
    CliNotFoundError,
)
from redpanda.cli.health import CliHealthChecker, CliProbe
from redpanda.cli.installer import ManifestCliInstaller
from redpanda.cli.models import (
    CliHealth,
    CliRecord,
    CliSourceRef,
    validate_cli_description,
    validate_cli_id,
)
from redpanda.cli.registry import CliRegistry
from redpanda.cli.runtime import CliToolCatalog
from redpanda.paths import RedPandaHome
from redpanda.sandbox.command import EnvironmentCommandExecutor


@dataclass(frozen=True)
class CliInstallCandidate:
    """propose 阶段冻结的登记身份；version/health 在执行时重新取最新事实。"""

    name: str
    description: str
    source: CliSourceRef
    resolved_path: str
    probed: CliProbe


@dataclass(frozen=True)
class CliTestResult:
    record: CliRecord
    probed: CliProbe


class CliApplicationService:
    """CLI 用户控制面：登记、检查、体检、刷新、修复与移除。"""

    def __init__(
        self,
        home: RedPandaHome,
        command_executor: EnvironmentCommandExecutor,
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
        self.installer = ManifestCliInstaller(command_executor, cwd=clis_root)
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
        resolved_path = await self.installer.resolve_path(locator)
        probed = await self.health_checker.probe(resolved_path)
        return CliInstallCandidate(
            name=name,
            description=description,
            source=CliSourceRef("manifest", locator),
            resolved_path=resolved_path,
            probed=probed,
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
            if not Path(resolved_path).is_file():
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
        """manifest 域 repair：重新解析 resolved_path 并重跑体检。"""
        async with self._management_lock:
            record = await self._require_at_revision(cli_id, expected_revision)
            resolved_path = await self.installer.resolve_path(
                record.source.locator
            )
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
        """移除登记。manifest 源不卸载软件本身——那是用户的手工安装。"""
        async with self._management_lock:
            await self._require_at_revision(cli_id, expected_revision)
            return await self.registry.remove(cli_id)

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
                f"CLI `{cli_id}` 登记已变化：expected revision "
                f"{expected_revision}, current revision {record.revision}"
            )
        return record
