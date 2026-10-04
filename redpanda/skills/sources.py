from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import io
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import urlparse
import zipfile

import httpx

from redpanda.skills.models import (
    SkillBundle,
    SkillPackageLimits,
    SkillSourceRef,
)
from redpanda.skills.errors import SkillInputError
from redpanda.skills.package import (
    LocalSkillPackageReader,
    SkillPackageError,
    validate_relative_skill_path,
)


class SkillSourceError(SkillInputError):
    pass


class SkillSourceRouter:
    def __init__(
        self,
        package_reader: LocalSkillPackageReader | None = None,
        *,
        timeout_seconds: float = 30.0,
        max_download_bytes: int = 25 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("Skill source timeout_seconds 必须大于 0")
        if type(max_download_bytes) is not int or max_download_bytes <= 0:
            raise ValueError("Skill source max_download_bytes 必须是正 int")
        self.package_reader = (
            LocalSkillPackageReader()
            if package_reader is None
            else package_reader
        )
        self.timeout_seconds = timeout_seconds
        self.max_download_bytes = max_download_bytes
        self.transport = transport

    async def fetch(self, source: SkillSourceRef) -> SkillBundle:
        if source.kind == "local":
            try:
                bundle = await asyncio.to_thread(
                    self.package_reader.read,
                    Path(source.locator),
                )
            except SkillPackageError as exc:
                raise SkillSourceError(
                    f"本地 Skill package 格式无效: {exc}"
                ) from exc
            except OSError as exc:
                raise SkillSourceError(
                    f"无法读取本地 Skill source: {source.locator}: {exc}"
                ) from exc
            return replace(bundle, source=source)
        if source.kind == "url":
            return await self._fetch_url(source)
        if source.kind == "github":
            return await self._fetch_github(source)
        raise SkillSourceError(f"不支持的 Skill source: {source.kind}")

    async def _fetch_url(self, source: SkillSourceRef) -> SkillBundle:
        self._validate_https_url(source.locator)
        content, final_url = await self._download(source.locator)
        try:
            if content.startswith(b"PK\x03\x04"):
                bundle = self._read_zip(content)
            else:
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / "SKILL.md").write_bytes(content)
                    bundle = self.package_reader.read(root)
        except SkillPackageError as exc:
            raise SkillSourceError(f"远程 Skill package 格式无效: {exc}") from exc
        return replace(
            bundle,
            source=source,
            resolved_ref=final_url,
        )

    async def _fetch_github(self, source: SkillSourceRef) -> SkillBundle:
        owner, repository, locator_ref, subpath = self._github_location(
            source.locator
        )
        if source.requested_ref is not None and locator_ref is not None:
            raise SkillSourceError(
                "GitHub tree URL 已包含 ref，不能再提供 requested_ref"
            )
        ref = (
            source.requested_ref
            if source.requested_ref is not None
            else locator_ref if locator_ref is not None else "HEAD"
        )
        api_url = f"https://api.github.com/repos/{owner}/{repository}/commits/{ref}"
        metadata_bytes, _ = await self._download(
            api_url,
            accept="application/vnd.github+json",
        )
        try:
            metadata = httpx.Response(200, content=metadata_bytes).json()
            commit = metadata["sha"]
            if type(commit) is not str or not commit:
                raise ValueError("GitHub commit sha 无效")
        except (KeyError, TypeError, ValueError) as exc:
            raise SkillSourceError("GitHub commit 响应缺少 sha") from exc
        archive_url = (
            f"https://codeload.github.com/{owner}/{repository}/zip/{commit}"
        )
        archive, _ = await self._download(archive_url)
        try:
            bundle = self._read_zip(archive, package_subpath=subpath)
        except SkillPackageError as exc:
            raise SkillSourceError(f"GitHub Skill package 格式无效: {exc}") from exc
        return replace(
            bundle,
            source=source,
            resolved_ref="".join([
                f"https://github.com/{owner}/{repository}/tree/{commit}",
                f"/{subpath}" if subpath else "",
            ]),
        )

    async def _download(
        self,
        url: str,
        *,
        accept: str | None = None,
    ) -> tuple[bytes, str]:
        headers = {"Accept": accept} if accept else None
        stack = AsyncExitStack()
        request_error: BaseException | None = None
        try:
            try:
                client = await stack.enter_async_context(httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ))
                response = await stack.enter_async_context(
                    client.stream("GET", url, headers=headers)
                )
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_download_bytes:
                        raise SkillSourceError("Skill source 下载超出大小限制")
                    chunks.append(chunk)
                return b"".join(chunks), str(response.url)
            except BaseException as exc:
                request_error = exc
                raise
            finally:
                try:
                    await stack.aclose()
                except BaseException as close_error:
                    if request_error is not None and close_error is not request_error:
                        raise BaseExceptionGroup(
                            "Skill 下载失败且关闭失败", [request_error, close_error]
                        )
                    raise
        except httpx.HTTPError as exc:
            host = urlparse(url).hostname or "unknown host"
            raise SkillSourceError(
                f"Skill source 下载失败：{host} ({type(exc).__name__})"
            ) from exc
        except BaseExceptionGroup as exc:
            if not _is_download_error(exc):
                raise
            host = urlparse(url).hostname or "unknown host"
            raise SkillSourceError(
                f"Skill source 下载或关闭失败：{host}"
            ) from exc

    def _read_zip(
        self,
        content: bytes,
        *,
        package_subpath: str | None = None,
    ) -> SkillBundle:
        limits = self.package_reader.limits
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise SkillSourceError("Skill source 不是有效 ZIP") from exc
        with archive, tempfile.TemporaryDirectory() as directory:
            all_infos = archive.infolist()
            for info in all_infos:
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise SkillSourceError(
                        f"Skill ZIP 不允许 symlink: {info.filename}"
                    )
            infos = [item for item in all_infos if not item.is_dir()]
            normalized: list[tuple[zipfile.ZipInfo, str]] = []
            for info in infos:
                raw = info.filename
                if "\\" in raw:
                    raise SkillSourceError(f"Skill ZIP 路径非法: {raw}")
                candidate = PurePosixPath(raw)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise SkillSourceError(f"Skill ZIP 路径非法: {raw}")
                normalized.append((info, candidate.as_posix()))
            if package_subpath is None:
                skill_files = [
                    path for _, path in normalized
                    if PurePosixPath(path).name == "SKILL.md"
                ]
            else:
                expected = PurePosixPath(
                    validate_relative_skill_path(package_subpath)
                ) / "SKILL.md"
                skill_files = []
                for _, raw in normalized:
                    path = PurePosixPath(raw)
                    without_archive_root = PurePosixPath(*path.parts[1:])
                    if without_archive_root == expected:
                        skill_files.append(raw)
            if len(skill_files) != 1:
                raise SkillSourceError(
                    "Skill ZIP 必须且只能包含一个 SKILL.md"
                )
            package_prefix = PurePosixPath(skill_files[0]).parent
            package_files: list[tuple[zipfile.ZipInfo, str]] = []
            for info, raw in normalized:
                try:
                    PurePosixPath(raw).relative_to(package_prefix)
                except ValueError:
                    continue
                package_files.append((info, raw))
            if len(package_files) > limits.max_files:
                raise SkillSourceError("Skill ZIP 超出文件数量限制")
            total = 0
            for info, raw in package_files:
                if info.file_size > limits.max_file_bytes:
                    raise SkillSourceError(f"Skill ZIP 文件超限: {raw}")
                total += info.file_size
                if total > limits.max_total_bytes:
                    raise SkillSourceError("Skill ZIP 超出总大小限制")
            root = Path(directory)
            for info, raw in package_files:
                path = PurePosixPath(raw)
                try:
                    relative = path.relative_to(package_prefix)
                except ValueError:
                    continue
                if relative.as_posix() == ".":
                    continue
                normalized_relative = validate_relative_skill_path(
                    relative.as_posix()
                )
                destination = root.joinpath(*PurePosixPath(
                    normalized_relative
                ).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    data = archive.read(info)
                except zipfile.BadZipFile as exc:
                    raise SkillSourceError(
                        f"Skill ZIP 内容损坏: {raw}"
                    ) from exc
                if len(data) != info.file_size:
                    raise SkillSourceError(f"Skill ZIP 文件长度不一致: {raw}")
                destination.write_bytes(data)
            return self.package_reader.read(root)

    @staticmethod
    def _validate_https_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SkillSourceError("remote Skill source 必须使用 HTTPS")

    @staticmethod
    def _github_location(
        locator: str,
    ) -> tuple[str, str, str | None, str | None]:
        value = locator.strip()
        if value.startswith("https://github.com/"):
            parts = urlparse(value).path.strip("/").split("/")
        else:
            parts = value.split("/")
        if len(parts) < 2 or not all(parts[:2]):
            raise SkillSourceError(
                "GitHub Skill locator 必须是 owner/repository "
                "或 GitHub tree URL"
            )
        owner, repository = parts[:2]
        if repository.endswith(".git"):
            repository = repository[:-4]
        if not repository:
            raise SkillSourceError("GitHub repository 不能为空")
        if len(parts) == 2:
            return owner, repository, None, None
        if len(parts) < 5 or parts[2] != "tree":
            raise SkillSourceError(
                "GitHub 子目录必须使用 /tree/<ref>/<subpath> URL"
            )
        ref = parts[3]
        subpath = PurePosixPath(*parts[4:]).as_posix()
        validate_relative_skill_path(subpath)
        return owner, repository, ref, subpath


def _is_download_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.HTTPError, SkillSourceError)):
        return True
    return isinstance(exc, BaseExceptionGroup) and all(
        _is_download_error(item) for item in exc.exceptions
    )
