"""github-release 安装源：从 GitHub Release 下载二进制，解压到 konden 自己的目录。

manifest 源只登记本机已有的 CLI；本模块补上"由 konden 提供"的那一半：
按 owner/repo（可选版本）取 Release，挑当前平台的归档，校验 sha256 后解压，
最后定位可执行文件。下载发生在审批通过之后，本模块不做任何授权判断。

归档只接受 zip / tar.gz；安装目录是 clis_root/_installs/<cli_id>/<version>/，
不写入 PATH，调用一律走 resolved_path 绝对路径。
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from konden.cli.errors import CliSourceError
from konden.cli.models import CliSourceRef

_GITHUB_API = "https://api.github.com"
_LOCATOR_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

_PLATFORM_TOKENS: dict[str, tuple[str, ...]] = {
    "windows": ("windows", "win64", "win32"),
    "darwin": ("darwin", "macos", "osx", "apple"),
    "linux": ("linux",),
}

_ARCH_TOKENS: dict[str, tuple[str, ...]] = {
    "x86_64": ("x86_64", "amd64", "x64"),
    "arm64": ("arm64", "aarch64"),
}

# 安装器、签名、SBOM 之类都不是可直接解压的归档。
_REJECTED_SUFFIXES = (
    ".msi",
    ".deb",
    ".rpm",
    ".pkg",
    ".dmg",
    ".apk",
    ".exe",
    ".msix",
    ".asc",
    ".sig",
    ".pem",
    ".txt",
    ".json",
    ".sha256",
    ".sbom",
    ".spdx",
    ".ps1",
    ".sh",
)

_CHECKSUM_HINTS = ("checksum", "sha256", "sha256sums")


@dataclass(frozen=True)
class ProvisionedCli:
    resolved_path: str
    version: str | None
    install_root: Path


async def provision_github_release(
    cli_id: str,
    source: CliSourceRef,
    *,
    install_root: Path,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = 60.0,
    max_download_bytes: int = 200 * 1024 * 1024,
) -> ProvisionedCli:
    """下载并解压一个 GitHub Release 资产，返回可执行文件绝对路径。"""
    owner, repository = _split_locator(source.locator)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout_seconds,
        transport=transport,
        headers={"Accept": "application/vnd.github+json"},
    ) as client:
        release = await _fetch_release(client, owner, repository, source.requested_version)
        tag = _require_str(release.get("tag_name"), "tag_name")
        version = tag.lstrip("vV")
        assets = _release_assets(release)
        asset = select_asset(assets, platform_key=_platform_key(), arch_key=_arch_key())
        payload = await _download(client, asset["browser_download_url"], max_download_bytes)
        checksum_asset = select_checksum_asset(assets)
        if checksum_asset is not None:
            checksums = _parse_checksums(
                (await _download(client, checksum_asset["browser_download_url"], max_download_bytes)).decode(
                    "utf-8", errors="replace"
                )
            )
            expected = checksums.get(str(asset["name"]).casefold())
            if expected is None:
                raise CliSourceError(f"校验文件里没有 {asset['name']} 的 sha256")
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected:
                raise CliSourceError(f"{asset['name']} 校验失败：期望 {expected}，实际 {actual}")

    destination = install_root / cli_id / _safe_segment(version)
    _reset_directory(destination)
    _extract(payload, str(asset["name"]), destination)
    executable = locate_executable(destination, cli_id)
    if executable is None:
        raise CliSourceError(
            f"{cli_id} 归档里找不到可执行文件：{asset['name']}"
        )
    return ProvisionedCli(resolved_path=str(executable), version=version, install_root=destination)


def select_asset(
    assets: list[dict[str, Any]],
    *,
    platform_key: str,
    arch_key: str,
) -> dict[str, Any]:
    """按平台与架构挑一个归档资产。

    名字里带**别的**平台/架构标识一律排除，只有完全不带标识的才当通用归档兜底；
    同分时优先 zip/tar.gz，再优先名字短的。
    """
    platform_tokens = _PLATFORM_TOKENS.get(platform_key, ())
    arch_tokens = _ARCH_TOKENS.get(arch_key, ())
    candidates: list[tuple[int, int, str, dict[str, Any]]] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        lowered = name.casefold()
        if not lowered or not asset.get("browser_download_url"):
            continue
        if lowered.endswith(_REJECTED_SUFFIXES) or any(hint in lowered for hint in _CHECKSUM_HINTS):
            continue
        archive_rank = 0 if lowered.endswith(".zip") else 1 if lowered.endswith(".tar.gz") else None
        if archive_rank is None:
            continue
        if _foreign_token(lowered, platform_tokens, _PLATFORM_TOKENS):
            continue
        if _foreign_token(lowered, arch_tokens, _ARCH_TOKENS):
            continue
        score = 0
        if platform_tokens and any(token in lowered for token in platform_tokens):
            score -= 4
        if arch_tokens and any(token in lowered for token in arch_tokens):
            score -= 2
        candidates.append((score + archive_rank, len(name), name, asset))
    if not candidates:
        raise CliSourceError(
            f"Release 里没有适用于 {platform_key}/{arch_key} 的 zip 或 tar.gz 资产"
        )
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]


def _foreign_token(
    lowered: str,
    wanted: tuple[str, ...],
    groups: dict[str, tuple[str, ...]],
) -> bool:
    """名字里出现别的分组的标识就排除；同组标识或完全不带标识都放行。"""
    if any(token in lowered for token in wanted):
        return False
    others = tuple(
        token for key, tokens in groups.items() if tokens is not wanted for token in tokens
    )
    return any(token in lowered for token in others)


def select_checksum_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    for asset in assets:
        name = str(asset.get("name") or "").casefold()
        if asset.get("browser_download_url") and any(hint in name for hint in _CHECKSUM_HINTS):
            return asset
    return None


def parse_checksums(text: str) -> dict[str, str]:
    """解析 `<sha256>  <filename>` 形式；返回 {小写文件名: 小写哈希}。"""
    return _parse_checksums(text)


def locate_executable(root: Path, cli_id: str) -> Path | None:
    """在解压目录里找可执行文件：优先 bin/ 下同名文件，其次同名文件，最后唯一的可执行文件。"""
    names = {cli_id, f"{cli_id}.exe"}
    preferred: list[Path] = []
    fallback: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not os.access(path, os.X_OK) and os.name != "nt":
            continue
        if path.name.casefold() in {name.casefold() for name in names}:
            preferred.append(path)
        elif _is_executable_name(path.name):
            fallback.append(path)
    for group in (preferred, fallback):
        if not group:
            continue
        in_bin = [path for path in group if path.parent.name == "bin"]
        return (in_bin or group)[0]
    return None


def _is_executable_name(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith(".exe") or "." not in lowered


def _split_locator(locator: str) -> tuple[str, str]:
    if not _LOCATOR_PATTERN.fullmatch(locator or ""):
        raise CliSourceError(
            f"github-release locator 必须是 owner/repo：{locator!r}"
        )
    owner, repository = locator.split("/", 1)
    return owner, repository


def _platform_key() -> str:
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _arch_key() -> str:
    machine = platform.machine().casefold()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


async def _fetch_release(
    client: httpx.AsyncClient,
    owner: str,
    repository: str,
    requested_version: str | None,
) -> dict[str, Any]:
    if requested_version:
        tag = requested_version if requested_version.startswith(("v", "V")) else f"v{requested_version}"
        url = f"{_GITHUB_API}/repos/{owner}/{repository}/releases/tags/{tag}"
    else:
        url = f"{_GITHUB_API}/repos/{owner}/{repository}/releases/latest"
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise CliSourceError(
            f"取 Release 失败：{owner}/{repository} HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise CliSourceError(
            f"取 Release 失败：{owner}/{repository} ({type(exc).__name__})。"
            "网络需要代理时请设置 HTTPS_PROXY。"
        ) from exc
    if not isinstance(payload, dict):
        raise CliSourceError("Release 响应不是 object")
    return payload


async def _download(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise CliSourceError("CLI 归档超出大小限制")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPStatusError as exc:
        raise CliSourceError(f"下载归档失败：HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise CliSourceError(f"下载归档失败：{type(exc).__name__}") from exc


def _release_assets(release: dict[str, Any]) -> list[dict[str, Any]]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        return []
    return [asset for asset in assets if isinstance(asset, dict)]


def _parse_checksums(text: str) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        digest, filename = parts[0], parts[-1].lstrip("*")
        if re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            checksums[filename.casefold()] = digest.casefold()
    return checksums


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]", "_", value).strip("._")
    if not cleaned:
        raise CliSourceError(f"版本号无法用作目录名：{value!r}")
    return cleaned


def _reset_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _extract(payload: bytes, name: str, destination: Path) -> None:
    lowered = name.casefold()
    if lowered.endswith(".zip"):
        _extract_zip(payload, destination)
    elif lowered.endswith(".tar.gz") or lowered.endswith(".tgz"):
        _extract_tar(payload, destination)
    else:
        raise CliSourceError(f"不支持的归档格式：{name}")


def _extract_zip(payload: bytes, destination: Path) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for info in archive.infolist():
                target = _safe_target(destination, info.filename)
                if target is None:
                    continue
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    target.chmod(mode)
                elif os.name != "nt":
                    # Windows 打的 zip 通常不带 Unix 权限位，解出来的 CLI 会被当成不可执行
                    target.chmod(0o755)
    except zipfile.BadZipFile as exc:
        raise CliSourceError("归档不是有效 ZIP") from exc


def _extract_tar(payload: bytes, destination: Path) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive.getmembers():
                target = _safe_target(destination, member.name)
                if target is None:
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                target.chmod(member.mode & 0o777 or 0o755)
    except tarfile.TarError as exc:
        raise CliSourceError("归档不是有效 tar.gz") from exc


def _safe_target(destination: Path, member_name: str) -> Path | None:
    """拒绝绝对路径与 .. 穿越；返回落盘目标。"""
    normalized = member_name.replace("\\", "/").lstrip("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        return None
    target = (destination / normalized).resolve()
    root = destination.resolve()
    if root != target and root not in target.parents:
        return None
    return target


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CliSourceError(f"Release 缺少 {label}")
    return value.strip()
