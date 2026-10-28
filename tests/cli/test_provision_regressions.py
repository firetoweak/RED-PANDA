"""独立验收发现的两个 P1 的回归测试：tag 前缀、校验文件配对。

两个缺陷都在真实仓库上复现过（ripgrep 用裸版本 tag，zellij 给每个资产配
`.sha256sum`），这里用 MockTransport 把形状钉住。
"""

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

from konden.cli.errors import CliSourceError
from konden.cli.models import CliSourceRef
from konden.cli.provision import (
    parse_checksums,
    provision_github_release,
    resolve_release_tag,
    select_checksum_asset,
)

ARCHIVE_NAME = "gh_1.0.0_windows_amd64.zip"


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o755 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def _asset(name: str) -> dict[str, str]:
    return {
        "name": name,
        "browser_download_url": f"https://example.test/{name}",
    }


def _release(tag: str, names: list[str]) -> dict[str, object]:
    return {"tag_name": tag, "assets": [_asset(name) for name in names]}


class ProvisionTransportTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.install_root = Path(self.temporary.name).resolve()
        self.archive = _zip_bytes({"bin/gh.exe": b"binary"})
        self.digest = hashlib.sha256(self.archive).hexdigest()
        # 资产名是 windows 的，把被测的平台探测固定住，测试才与 runner 无关
        self._platform_patch = patch(
            "konden.cli.provision._platform_key", return_value="windows"
        )
        self._arch_patch = patch(
            "konden.cli.provision._arch_key", return_value="x86_64"
        )
        self._platform_patch.start()
        self._arch_patch.start()

    def tearDown(self) -> None:
        self._platform_patch.stop()
        self._arch_patch.stop()
        self.temporary.cleanup()

    def _transport(self, handler) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    def _serve(self, release_by_tag: dict[str, dict], files: dict[str, bytes]):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            for tag, release in release_by_tag.items():
                if url.endswith(f"/releases/tags/{tag}"):
                    return httpx.Response(200, json=release)
            for name, payload in files.items():
                if url.endswith(f"/{name}"):
                    return httpx.Response(200, content=payload)
            return httpx.Response(404)

        return self._transport(handler)

    async def _provision(self, version: str, transport: httpx.MockTransport):
        return await provision_github_release(
            "gh",
            CliSourceRef("github-release", "cli/cli", version),
            install_root=self.install_root,
            transport=transport,
        )


class TagPrefixTest(ProvisionTransportTest):
    async def test_tag_without_v_prefix_installs(self) -> None:
        """ripgrep 这类仓库的 tag 是 `15.2.0`，不能强行补 v。"""
        transport = self._serve(
            {"1.0.0": _release("1.0.0", [ARCHIVE_NAME])},
            {ARCHIVE_NAME: self.archive},
        )
        provisioned = await self._provision("1.0.0", transport)
        self.assertEqual(provisioned.version, "1.0.0")
        self.assertTrue(provisioned.resolved_path.endswith("gh.exe"))

    async def test_v_prefixed_repo_still_matches_by_fallback(self) -> None:
        """用户手写 `1.0.0`、仓库实际 tag 是 `v1.0.0`：第二个候选要能命中。"""
        transport = self._serve(
            {"v1.0.0": _release("v1.0.0", [ARCHIVE_NAME])},
            {ARCHIVE_NAME: self.archive},
        )
        tag = await resolve_release_tag(
            CliSourceRef("github-release", "cli/cli", "1.0.0"), transport=transport
        )
        provisioned = await self._provision(tag, transport)
        self.assertEqual(provisioned.version, "1.0.0")

    async def test_missing_requested_version_reports_both_candidates(self) -> None:
        transport = self._serve({}, {})
        with self.assertRaises(CliSourceError) as caught:
            await resolve_release_tag(
                CliSourceRef("github-release", "cli/cli", "9.9.9"), transport=transport
            )
        message = str(caught.exception)
        self.assertIn("9.9.9", message)
        self.assertIn("v9.9.9", message)

    async def test_withdrawn_frozen_tag_does_not_install_an_alternative(self) -> None:
        """前缀相近的 tag 是不同 Release，不能替用户批准另一个版本。"""
        transport = self._serve(
            {"1.0.0": _release("1.0.0", [ARCHIVE_NAME])},
            {ARCHIVE_NAME: self.archive},
        )
        with self.assertRaises(CliSourceError):
            await self._provision("v1.0.0", transport)
        self.assertFalse((self.install_root / "gh").exists())


class ChecksumPairingTest(ProvisionTransportTest):
    async def test_companion_checksum_matches_chosen_asset(self) -> None:
        """zellij 式：每个归档各配一个 .sha256sum，必须取选中归档的那一个。"""
        companion = f"{ARCHIVE_NAME}.sha256sum"
        assets = [
            _asset("gh_1.0.0_aarch64-apple-darwin.tar.gz.sha256sum"),
            _asset(ARCHIVE_NAME),
            _asset(companion),
        ]
        chosen = select_checksum_asset(assets, ARCHIVE_NAME)
        self.assertEqual(chosen["name"], companion)

    async def test_companion_content_is_used_for_verification(self) -> None:
        companion = f"{ARCHIVE_NAME}.sha256"
        for checksum in (
            f"{self.digest}  {ARCHIVE_NAME}\n",
            f"{self.digest}\n",
            f"SHA256 hash of {ARCHIVE_NAME}:\r\n{self.digest}\r\n"
            "CertUtil: -hashfile command completed successfully.\r\n",
        ):
            with self.subTest(checksum=checksum):
                transport = self._serve(
                    {"v1.0.0": _release("v1.0.0", [ARCHIVE_NAME, companion])},
                    {companion: checksum.encode(), ARCHIVE_NAME: self.archive},
                )
                provisioned = await self._provision("v1.0.0", transport)
                self.assertTrue(provisioned.resolved_path.endswith("gh.exe"))

    async def test_stem_style_companion_is_matched(self) -> None:
        """zellij 式：校验文件把归档扩展名去掉，如 `tool-x86_64-pc-windows-msvc.sha256sum`。"""
        assets = [
            _asset("gh_1.0.0_aarch64-apple-darwin.sha256sum"),
            _asset(ARCHIVE_NAME),
            _asset("gh_1.0.0_windows_amd64.sha256sum"),
        ]
        chosen = select_checksum_asset(assets, ARCHIVE_NAME)
        self.assertEqual(chosen["name"], "gh_1.0.0_windows_amd64.sha256sum")

    async def test_aggregate_checksum_file_still_works(self) -> None:
        transport = self._serve(
            {"v1.0.0": _release("v1.0.0", [ARCHIVE_NAME, "checksums.txt"])},
            {
                "checksums.txt": f"{self.digest}  {ARCHIVE_NAME}\n".encode(),
                ARCHIVE_NAME: self.archive,
            },
        )
        provisioned = await self._provision("v1.0.0", transport)
        self.assertTrue(provisioned.resolved_path.endswith("gh.exe"))

    async def test_wrong_digest_in_companion_fails_before_extract(self) -> None:
        companion = f"{ARCHIVE_NAME}.sha256"
        for checksum in (
            f"{'0' * 64}  {ARCHIVE_NAME}\n",
            "0" * 64,
            f"SHA256 hash of {ARCHIVE_NAME}:\r\n{'0' * 64}\r\n"
            "CertUtil: -hashfile command completed successfully.\r\n",
        ):
            with self.subTest(checksum=checksum):
                transport = self._serve(
                    {"v1.0.0": _release("v1.0.0", [ARCHIVE_NAME, companion])},
                    {companion: checksum.encode(), ARCHIVE_NAME: self.archive},
                )
                with self.assertRaises(CliSourceError) as caught:
                    await self._provision("v1.0.0", transport)
                self.assertIn("校验失败", str(caught.exception))
                self.assertFalse((self.install_root / "gh").exists())

    async def test_single_entry_companion_may_name_inner_path(self) -> None:
        """zellij 的 .sha256sum 写的是归档内路径，只能在解压后校验该文件。"""
        companion = f"{ARCHIVE_NAME}.sha256sum"
        inner_digest = hashlib.sha256(b"binary").hexdigest()
        transport = self._serve(
            {"v1.0.0": _release("v1.0.0", [ARCHIVE_NAME, companion])},
            {
                companion: f"{inner_digest}  bin/gh.exe\n".encode(),
                ARCHIVE_NAME: self.archive,
            },
        )
        provisioned = await self._provision("v1.0.0", transport)
        self.assertTrue(provisioned.resolved_path.endswith("gh.exe"))

    async def test_inner_file_mismatch_is_rejected(self) -> None:
        installed = await self._provision(
            "v1.0.0",
            self._serve(
                {"v1.0.0": _release("v1.0.0", [ARCHIVE_NAME])},
                {ARCHIVE_NAME: self.archive},
            ),
        )
        companion = f"{ARCHIVE_NAME}.sha256sum"
        transport = self._serve(
            {"v1.0.0": _release("v1.0.0", [ARCHIVE_NAME, companion])},
            {
                companion: f"{'0' * 64}  bin/gh.exe\n".encode(),
                ARCHIVE_NAME: _zip_bytes({"bin/gh.exe": b"rejected replacement"}),
            },
        )
        with self.assertRaises(CliSourceError) as caught:
            await self._provision("v1.0.0", transport)
        self.assertIn("校验失败", str(caught.exception))
        self.assertEqual(Path(installed.resolved_path).read_bytes(), b"binary")
        self.assertEqual(list(installed.install_root.parent.iterdir()), [installed.install_root])

    async def test_inner_path_missing_from_archive_is_reported(self) -> None:
        companion = f"{ARCHIVE_NAME}.sha256sum"
        transport = self._serve(
            {"v1.0.0": _release("v1.0.0", [ARCHIVE_NAME, companion])},
            {
                companion: f"{'0' * 64}  target/release/nope.exe\n".encode(),
                ARCHIVE_NAME: self.archive,
            },
        )
        with self.assertRaises(CliSourceError) as caught:
            await self._provision("v1.0.0", transport)
        self.assertIn("没有找到", str(caught.exception))

    def test_parse_checksums_ignores_non_digest_lines(self) -> None:
        parsed = parse_checksums(f"{self.digest}  {ARCHIVE_NAME}\nnot a line\n")
        self.assertEqual(parsed[ARCHIVE_NAME], self.digest)


class RateLimitTest(ProvisionTransportTest):
    async def test_rate_limit_is_distinguishable_from_missing_tag(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"},
                json={"message": "API rate limit exceeded"},
            )

        with self.assertRaises(CliSourceError) as caught:
            await self._provision("v1.0.0", self._transport(handler))
        self.assertIn("限流", str(caught.exception))


class AbsoluteMemberTest(ProvisionTransportTest):
    async def test_absolute_member_is_rejected_not_rewritten(self) -> None:
        archive = _zip_bytes({"bin/gh.exe": b"binary", "/abs.txt": b"nope"})
        transport = self._serve(
            {"v1.0.0": _release("v1.0.0", [ARCHIVE_NAME])},
            {ARCHIVE_NAME: archive},
        )
        provisioned = await self._provision("v1.0.0", transport)
        self.assertTrue(provisioned.resolved_path.endswith("gh.exe"))
        self.assertFalse((self.install_root / "abs.txt").exists())
        self.assertFalse((Path(provisioned.install_root) / "abs.txt").exists())
