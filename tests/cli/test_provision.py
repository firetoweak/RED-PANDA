"""github-release 安装源的离线用例：资产挑选、校验文件、解压边界与可执行文件定位。

全部用例只用现场构造的伪造 Release 与内存归档，不发任何网络请求；
唯一走 HTTP 的路径用 httpx.MockTransport 注入，仍然不出进程。
"""

import hashlib
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

from konden.cli import provision
from konden.cli.errors import CliSourceError
from konden.cli.models import CliSourceRef
from konden.cli.provision import (
    _extract,
    _parse_checksums,
    _safe_target,
    locate_executable,
    parse_checksums,
    select_asset,
)

_ARCHIVE_MODE = 0o755
_TIMESTAMP = (2024, 1, 1, 0, 0, 0)

_RELEASE_ASSET_NAMES = (
    "gh_2.40.0_windows_amd64.zip",
    "gh_2.40.0_windows_arm64.zip",
    "gh_2.40.0_linux_amd64.tar.gz",
    "gh_2.40.0_linux_arm64.tar.gz",
    "gh_2.40.0_macOS_arm64.zip",
    "gh_2.40.0_macOS_amd64.zip",
    "gh_2.40.0_windows_amd64.msi",
    "gh_2.40.0_linux_amd64.deb",
    "gh_2.40.0_checksums.txt",
    "gh_2.40.0_windows_amd64.zip.sig",
)


def make_assets(names=_RELEASE_ASSET_NAMES) -> list[dict[str, object]]:
    return [
        {"name": name, "browser_download_url": f"https://example.invalid/{name}"}
        for name in names
    ]


def make_zip(entries: dict[str, bytes], *, mode: int = _ARCHIVE_MODE) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            info = zipfile.ZipInfo(name, date_time=_TIMESTAMP)
            info.external_attr = mode << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def make_tar_gz(entries: dict[str, bytes], *, mode: int = _ARCHIVE_MODE) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = mode
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def write_binary(path: Path, payload: bytes = b"binary") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(_ARCHIVE_MODE)
    return path


class ReleaseAssetSelectionTest(unittest.IsolatedAsyncioTestCase):
    def test_windows_x86_64_picks_windows_zip(self):
        asset = select_asset(make_assets(), platform_key="windows", arch_key="x86_64")
        self.assertEqual(asset["name"], "gh_2.40.0_windows_amd64.zip")

    def test_darwin_arm64_picks_macos_arm64(self):
        asset = select_asset(make_assets(), platform_key="darwin", arch_key="arm64")
        self.assertEqual(asset["name"], "gh_2.40.0_macOS_arm64.zip")

    def test_linux_x86_64_picks_linux_tarball(self):
        asset = select_asset(make_assets(), platform_key="linux", arch_key="x86_64")
        self.assertEqual(asset["name"], "gh_2.40.0_linux_amd64.tar.gz")

    def test_installers_and_signatures_are_not_archives(self):
        # 只有 .msi / .deb / checksums / .sig 时没有可用归档。
        assets = make_assets(
            name
            for name in _RELEASE_ASSET_NAMES
            if not name.endswith((".zip", ".tar.gz"))
        )
        with self.assertRaises(CliSourceError) as context:
            select_asset(assets, platform_key="windows", arch_key="x86_64")
        self.assertIn("windows/x86_64", str(context.exception))

    def test_other_platform_only_assets_raise(self):
        assets = make_assets(["gh_2.40.0_linux_amd64.tar.gz"])
        with self.assertRaises(CliSourceError):
            select_asset(assets, platform_key="windows", arch_key="x86_64")

    def test_arm64_asset_is_not_offered_to_x86_64(self):
        assets = make_assets(["gh_2.40.0_linux_arm64.tar.gz"])
        with self.assertRaises(CliSourceError):
            select_asset(assets, platform_key="linux", arch_key="x86_64")

    def test_asset_without_download_url_is_skipped(self):
        assets = [{"name": "gh_2.40.0_windows_amd64.zip"}]
        with self.assertRaises(CliSourceError):
            select_asset(assets, platform_key="windows", arch_key="x86_64")


class ChecksumFileTest(unittest.IsolatedAsyncioTestCase):
    def test_plain_and_star_filenames_are_parsed(self):
        first = "a" * 64
        second = "B" * 64
        text = "\n".join(
            [
                f"{first}  gh_2.40.0_linux_amd64.tar.gz",
                f"{second} *gh_2.40.0_windows_amd64.zip",
                "",
            ]
        )
        self.assertEqual(
            parse_checksums(text),
            {
                "gh_2.40.0_linux_amd64.tar.gz": first,
                "gh_2.40.0_windows_amd64.zip": second.casefold(),
            },
        )
        self.assertEqual(_parse_checksums(text), parse_checksums(text))

    def test_illegal_lines_are_ignored(self):
        valid = "c" * 64
        text = "\n".join(
            [
                "not-a-digest  gh_2.40.0_linux_amd64.tar.gz",
                f"{'d' * 63}  gh_2.40.0_windows_amd64.zip",
                "only-one-field",
                "",
                f"{valid}  gh_2.40.0_macOS_arm64.zip",
            ]
        )
        self.assertEqual(
            parse_checksums(text),
            {"gh_2.40.0_macos_arm64.zip": valid},
        )


class ArchiveExtractionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.destination = self.root / "install"

    def tearDown(self):
        self.temporary.cleanup()

    def test_zip_is_extracted_to_destination(self):
        payload = make_zip(
            {
                "bin/gh.exe": b"windows-binary",
                "LICENSE": b"MIT",
            }
        )
        _extract(payload, "gh_2.40.0_windows_amd64.zip", self.destination)
        self.assertEqual(
            (self.destination / "bin" / "gh.exe").read_bytes(), b"windows-binary"
        )
        self.assertEqual((self.destination / "LICENSE").read_bytes(), b"MIT")

    def test_tar_gz_is_extracted_to_destination(self):
        payload = make_tar_gz({"gh": b"unix-binary", "LICENSE": b"MIT"})
        _extract(payload, "gh_2.40.0_linux_amd64.tar.gz", self.destination)
        self.assertEqual((self.destination / "gh").read_bytes(), b"unix-binary")
        self.assertEqual((self.destination / "LICENSE").read_bytes(), b"MIT")

    def test_parent_traversal_member_is_rejected(self):
        payload = make_zip(
            {
                "bin/gh.exe": b"windows-binary",
                "../evil.txt": b"pwned",
                "../../evil.txt": b"pwned",
                "bin/../../evil.txt": b"pwned",
            }
        )
        _extract(payload, "gh_2.40.0_windows_amd64.zip", self.destination)
        self.assertTrue((self.destination / "bin" / "gh.exe").is_file())
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertFalse((self.destination.parent / "evil.txt").exists())
        self.assertFalse((self.root.parent / "evil.txt").exists())

    def test_tar_parent_traversal_member_is_rejected(self):
        payload = make_tar_gz({"gh": b"unix-binary", "../evil.txt": b"pwned"})
        _extract(payload, "gh_2.40.0_linux_amd64.tar.gz", self.destination)
        self.assertTrue((self.destination / "gh").is_file())
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertFalse((self.destination.parent / "evil.txt").exists())

    def test_absolute_member_stays_inside_destination(self):
        # 前导 "/" 会被剥掉落到安装目录内，而不是写到根目录；断言边界仍然有效。
        payload = make_zip({"/outside.txt": b"pwned", "bin/gh.exe": b"windows-binary"})
        _extract(payload, "gh_2.40.0_windows_amd64.zip", self.destination)
        self.assertTrue((self.destination / "bin" / "gh.exe").is_file())
        self.assertFalse(Path("/outside.txt").exists())
        self.assertFalse((self.root / "outside.txt").exists())

    def test_safe_target_rejects_traversal_names(self):
        for name in (
            "../evil.txt",
            "../../evil.txt",
            "bin/../../evil.txt",
            "a\\..\\..\\evil.txt",
            "..",
            "",
        ):
            with self.subTest(name=name):
                self.assertIsNone(_safe_target(self.destination, name))

    def test_safe_target_keeps_plain_names_inside(self):
        target = _safe_target(self.destination, "bin/gh")
        self.assertEqual(target, (self.destination / "bin" / "gh").resolve())

    def test_windows_drive_path_does_not_escape(self):
        target = _safe_target(self.destination, "C:\\evil.txt")
        if target is not None:
            self.assertIn(self.destination.resolve(), target.parents)

    def test_unsupported_archive_format_raises(self):
        with self.assertRaises(CliSourceError):
            _extract(b"installer", "gh_2.40.0_windows_amd64.msi", self.destination)

    def test_broken_zip_raises(self):
        with self.assertRaises(CliSourceError):
            _extract(b"not a zip", "gh.zip", self.destination)

    def test_broken_tar_gz_raises(self):
        with self.assertRaises(CliSourceError):
            _extract(b"not a tar", "gh.tar.gz", self.destination)


class LocateExecutableTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_bin_directory_wins_over_root_copy(self):
        write_binary(self.root / "gh.exe")
        write_binary(self.root / "bin" / "gh.exe")
        self.assertEqual(
            locate_executable(self.root, "gh"), self.root / "bin" / "gh.exe"
        )

    def test_executable_at_root_is_found(self):
        write_binary(self.root / "gh.exe")
        self.assertEqual(locate_executable(self.root, "gh"), self.root / "gh.exe")

    def test_executable_is_found_after_zip_extraction(self):
        payload = make_zip({"bin/gh.exe": b"windows-binary", "README.md": b"docs"})
        _extract(payload, "gh_2.40.0_windows_amd64.zip", self.root)
        self.assertEqual(
            locate_executable(self.root, "gh"), self.root / "bin" / "gh.exe"
        )

    def test_uniquely_named_binary_is_used_as_fallback(self):
        binary = write_binary(self.root / "tools" / "gh-cli.exe")
        self.assertEqual(locate_executable(self.root, "gh"), binary)

    def test_windows_alias_ignores_extensionless_documents(self):
        """登记名不等于程序名时，不能把许可证登记成 CLI。"""
        binary = write_binary(self.root / "rg.exe")
        write_binary(self.root / "LICENSE-MIT", b"license")
        with patch.object(provision, "_platform_key", return_value="windows"):
            self.assertEqual(locate_executable(self.root, "ripgrep"), binary)

    def test_ambiguous_fallback_is_rejected(self):
        write_binary(self.root / "client.exe")
        write_binary(self.root / "server.exe")
        self.assertIsNone(locate_executable(self.root, "tool"))

    def test_missing_executable_returns_none(self):
        (self.root / "README.md").write_text("docs", encoding="utf-8")
        (self.root / "checksums.txt").write_text("", encoding="utf-8")
        self.assertIsNone(locate_executable(self.root, "gh"))


class CliSourceRefTest(unittest.IsolatedAsyncioTestCase):
    def test_github_release_locator_is_accepted(self):
        source = CliSourceRef(kind="github-release", locator="cli/cli")
        self.assertEqual(source.kind, "github-release")
        self.assertEqual(source.locator, "cli/cli")
        self.assertIsNone(source.requested_version)
        self.assertEqual(
            CliSourceRef.from_dict(source.to_dict()),
            CliSourceRef(kind="github-release", locator="cli/cli", requested_version=None),
        )

    def test_requested_version_roundtrips(self):
        source = CliSourceRef(
            kind="github-release", locator="cli/cli", requested_version="2.40.0"
        )
        self.assertEqual(CliSourceRef.from_dict(source.to_dict()), source)

    def test_locator_must_be_owner_repo(self):
        for locator in ("not-a-repo", "a/b/c", "/cli", "cli/", "cli/cli extra", ""):
            with self.subTest(locator=locator):
                with self.assertRaises(ValueError):
                    CliSourceRef(kind="github-release", locator=locator)

    def test_manifest_source_is_unchanged(self):
        source = CliSourceRef(kind="manifest", locator="rg.json")
        self.assertEqual(source.to_dict()["kind"], "manifest")
        self.assertEqual(CliSourceRef.from_dict(source.to_dict()), source)
        with self.assertRaises(ValueError):
            CliSourceRef(kind="manifest", locator="   ")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            CliSourceRef(kind="release", locator="cli/cli")


class GithubReleaseProvisionTest(unittest.IsolatedAsyncioTestCase):
    """下载、校验、解压、定位的整条链路，用 MockTransport 顶替网络。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.install_root = self.root / "_installs"

    def tearDown(self):
        self.temporary.cleanup()

    def _release_transport(
        self,
        archive: bytes,
        checksums: str,
        requested: list[str],
        release_path: str = "/repos/cli/cli/releases/latest",
    ) -> httpx.MockTransport:
        assets = [
            {
                "name": "gh_2.40.0_linux_amd64.tar.gz",
                "browser_download_url": "https://example.invalid/gh.tar.gz",
            },
            {
                "name": "checksums.txt",
                "browser_download_url": "https://example.invalid/checksums.txt",
            },
        ]

        tag = release_path.rsplit("/", 1)[-1] if "/tags/" in release_path else "v2.40.0"

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            if request.url.path == release_path:
                return httpx.Response(200, json={"tag_name": tag, "assets": assets})
            if request.url.path == "/gh.tar.gz":
                return httpx.Response(200, content=archive)
            if request.url.path == "/checksums.txt":
                return httpx.Response(200, text=checksums)
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    async def _provision(
        self,
        transport: httpx.MockTransport,
        *,
        requested_version: str | None = None,
    ):
        source = CliSourceRef(
            kind="github-release", locator="cli/cli", requested_version=requested_version
        )
        with (
            patch.object(provision, "_platform_key", return_value="linux"),
            patch.object(provision, "_arch_key", return_value="x86_64"),
        ):
            return await provision.provision_github_release(
                "gh",
                source,
                install_root=self.install_root,
                transport=transport,
            )

    async def test_verified_archive_is_installed_under_version_directory(self):
        archive = make_tar_gz({"gh": b"unix-binary", "LICENSE": b"MIT"})
        digest = hashlib.sha256(archive).hexdigest()
        requested: list[str] = []
        transport = self._release_transport(
            archive,
            f"{digest}  gh_2.40.0_linux_amd64.tar.gz\n",
            requested,
        )

        result = await self._provision(transport)

        self.assertEqual(result.version, "2.40.0")
        self.assertEqual(result.install_root, self.install_root / "gh" / "2.40.0")
        self.assertEqual(
            Path(result.resolved_path),
            self.install_root / "gh" / "2.40.0" / "gh",
        )
        self.assertEqual(Path(result.resolved_path).read_bytes(), b"unix-binary")
        self.assertIn("/repos/cli/cli/releases/latest", requested)

    async def test_checksum_mismatch_fails_before_install(self):
        archive = make_tar_gz({"gh": b"unix-binary"})
        requested: list[str] = []
        transport = self._release_transport(
            archive,
            f"{'0' * 64}  gh_2.40.0_linux_amd64.tar.gz\n",
            requested,
            release_path="/repos/cli/cli/releases/tags/v2.39.0",
        )

        with self.assertRaises(CliSourceError) as context:
            await self._provision(transport, requested_version="v2.39.0")

        self.assertIn("校验失败", str(context.exception))
        self.assertIn("/repos/cli/cli/releases/tags/v2.39.0", requested)
        self.assertFalse((self.install_root / "gh").exists())


if __name__ == "__main__":
    unittest.main()
