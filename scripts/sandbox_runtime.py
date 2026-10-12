"""打包或安装预编译沙箱；运行包安装不调用编译工具。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"windows-x86_64": "redpanda-sandbox.exe", "linux-x86_64": "redpanda-sandbox"}


def host_platform() -> str:
    name = f"{platform.system().lower()}-x86_64"
    if platform.machine().lower() not in {"amd64", "x86_64"} or name not in PLATFORMS:
        raise ValueError("沙箱运行包目前只支持 Windows x64 和 Linux x64。")
    if name == "linux-x86_64":
        libc, version = platform.libc_ver()
        if libc != "glibc" or not version or tuple(map(int, version.split("."))) < (2, 35):
            raise ValueError("Linux 预编译运行包需要 glibc 2.35+；其他环境可自行构建原生组件。")
    return name


def release_config(root: Path) -> dict:
    config = json.loads((root / "native/sandbox/release.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", config["version"]):
        raise ValueError("沙箱运行包版本必须为 major.minor.patch。")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", config["repository"]):
        raise ValueError("沙箱运行包仓库必须为 owner/repository。")
    return config


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def manifest_files(record: dict, target: str) -> set[str]:
    files = record["files"]
    required = {PLATFORMS[target]}
    if target == "windows-x86_64":
        required.update({"winfsp-x64.dll", "libunwind.dll", "licenses/WINFSP-LICENSE.txt", "licenses/LLVM-LICENSE.txt"})
    if not required.issubset(files) or not any(name.startswith("licenses/") for name in files):
        raise ValueError("沙箱运行包缺少程序、运行库或许可证。")
    for name, expected in files.items():
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
                or ":" in name or path.as_posix() != name or name == "BUILD.json"
                or any(part.endswith((".", " ")) for part in path.parts)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError(f"沙箱运行包清单无效：{name}")
    return set(files)


def verify(directory: Path, record: dict, target: str) -> None:
    for name in manifest_files(record, target):
        path = directory / name
        if not path.is_file() or path.is_symlink() or digest(path) != record["files"][name]:
            raise ValueError(f"沙箱文件缺失或校验失败：{name}")


def package(root: Path, output: Path, target: str) -> Path:
    config = release_config(root)
    source = root / "redpanda/sandbox/bin"
    record = json.loads((source / "BUILD.json").read_text(encoding="utf-8"))
    if record["profile"] != "release":
        raise ValueError("发布运行包必须先完成 release 构建。")
    expected_target = "x86_64-pc-windows-gnullvm" if target.startswith("windows") else "x86_64-unknown-linux-gnu"
    if record["target"] != expected_target:
        raise ValueError(f"构建目标不匹配：需要 {expected_target}。")
    verify(source, record, target)
    record["package"] = {"version": config["version"], "platform": target}
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"redpanda-sandbox-{target}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("BUILD.json", json.dumps(record, ensure_ascii=False, indent=2))
        for name in sorted(record["files"]):
            bundle.write(source / name, name)
    archive.with_suffix(".zip.sha256").write_text(f"{digest(archive)}  {archive.name}\n", encoding="ascii", newline="\n")
    print(f"已打包：{archive}")
    return archive


def download(url: str, destination: Path) -> None:
    print(f"下载 {url}", flush=True)
    try:
        with urlopen(url, timeout=120) as response, destination.open("wb") as stream:
            shutil.copyfileobj(response, stream)
    except HTTPError as error:
        if error.code == 404:
            raise ValueError("当前版本的沙箱运行包尚未发布，请查看仓库 Releases；无需安装 Rust 来运行。") from error
        raise


def install(root: Path, *, replace: bool = False) -> Path:
    target = host_platform()
    configured = os.environ.get("REDPANDA_SANDBOX_EXECUTABLE")
    if configured is not None:
        executable = Path(configured).resolve()
        if not executable.is_file():
            raise ValueError(f"REDPANDA_SANDBOX_EXECUTABLE 指定的程序不存在：{executable}")
        print(f"使用指定沙箱：{executable}")
        return executable
    config = release_config(root)
    destination = root / "redpanda/sandbox/bin"
    executable = destination / PLATFORMS[target]
    record_path = destination / "BUILD.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() and not replace else {}
    if executable.is_file() and "package" not in record and not replace:
        print(f"保留本地构建的沙箱：{executable}")
        return executable
    if "package" in record and not replace:
        try:
            verify(destination, record, target)
        except ValueError as error:
            raise ValueError(f"{error}；为保留本地修改，已停止覆盖。确认重新下载时使用 install --replace。") from error
        if record["package"] == {"version": config["version"], "platform": target}:
            print(f"沙箱运行包 {config['version']} 已就绪，跳过下载。")
            return executable
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".sandbox-", dir=destination.parent))
    backup = staging / "previous"
    try:
        archive = staging / f"redpanda-sandbox-{target}.zip"
        base = f"https://github.com/{config['repository']}/releases/download/sandbox-v{config['version']}"
        download(f"{base}/{archive.name}", archive)
        checksum = staging / "checksum"
        download(f"{base}/{archive.name}.sha256", checksum)
        if checksum.read_text(encoding="ascii").split() != [digest(archive), archive.name]:
            raise ValueError("沙箱下载包 SHA256 校验失败；原有程序未更改。")
        unpacked = staging / "bin"
        unpacked.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            record = json.loads(bundle.read("BUILD.json"))
            if record.get("package") != {"version": config["version"], "platform": target}:
                raise ValueError("下载包版本或平台与当前源码不匹配。")
            names = [entry.filename for entry in bundle.infolist()]
            expected = manifest_files(record, target) | {"BUILD.json"}
            if len(names) != len({name.casefold() for name in names}) or set(names) != expected:
                raise ValueError("沙箱压缩包内容与清单不一致。")
            bundle.extractall(unpacked)
        verify(unpacked, record, target)
        (unpacked / PLATFORMS[target]).chmod(0o755)
        if destination.exists():
            destination.rename(backup)
        try:
            unpacked.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        # 恢复失败时保留备份，供用户取回，不能随临时目录一起删除。
        if not backup.exists():
            shutil.rmtree(staging)
    print(f"沙箱运行包 {config['version']} 已安装：{executable}")
    return executable


def ensure_platform_runtime(root: Path) -> None:
    if sys.platform == "win32":
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(root / "scripts/install_winfsp.ps1")], check=True)
    elif sys.platform == "linux":
        if shutil.which("fusermount3") is None:
            raise ValueError("缺少 FUSE 运行组件：Ubuntu/Debian 执行 sudo apt-get install -y fuse3；Fedora 执行 sudo dnf install -y fuse3。")
        if not os.access("/dev/fuse", os.R_OK | os.W_OK) or not os.access("/etc/mtab", os.R_OK):
            raise ValueError("FUSE 运行条件未就绪：需可读写 /dev/fuse 且可读 /etc/mtab，见 native/sandbox/README.md。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    installer = actions.add_parser("install", help="安装或复用沙箱运行包")
    installer.add_argument("--replace", action="store_true", help="用发布包替换本地沙箱，包括本地构建")
    installer.add_argument("--with-runtime", action="store_true", help="同时检查 FUSE，或安装 WinFsp 驱动（不含 SDK）")
    packager = actions.add_parser("package", help="将 release 构建打包供发布")
    packager.add_argument("--platform", choices=PLATFORMS, required=True)
    packager.add_argument("--output", type=Path, default=ROOT / "dist/sandbox")
    args = parser.parse_args(argv)
    try:
        if args.action == "package":
            package(ROOT, args.output, args.platform)
        else:
            install(ROOT, replace=args.replace)
            if args.with_runtime:
                ensure_platform_runtime(ROOT)
    except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        parser.exit(1, f"沙箱准备失败：{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
