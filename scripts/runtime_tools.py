"""Prepare application-local command line tools without changing the user's PATH."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def host_platform() -> str:
    name = f"{platform.system().lower()}-x86_64"
    if platform.machine().lower() not in {"amd64", "x86_64"} or name not in {"windows-x86_64", "linux-x86_64"}:
        raise ValueError("应用工具目前只支持 Windows x64 和 Linux x64。")
    return name


def packages(root: Path) -> dict:
    return json.loads((root / "scripts/runtime_tools.json").read_text(encoding="utf-8"))[host_platform()]


def tool_directory(root: Path, name: str) -> Path:
    return root / ".tools/runtime" / name


def activate(root: Path) -> None:
    directories = [tool_directory(root, name) / package["bin"]
                   for name, package in packages(root).items()]
    # Only the current process and its children receive these paths.
    active = [str(path) for path in directories if path.is_dir()]
    inherited = [path for path in os.environ.get("PATH", "").split(os.pathsep) if path not in active]
    os.environ["PATH"] = os.pathsep.join(active + inherited)


def supported_node() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    version = subprocess.check_output([node, "--version"], text=True).strip()
    major, minor, _ = map(int, version.removeprefix("v").split("."))
    return major == 24 or major == 22 and minor >= 13


def install_package(root: Path, name: str, package: dict) -> None:
    destination = tool_directory(root, name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{name}-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        archive = staging / package["url"].rsplit("/", 1)[-1]
        print(f"下载 {name} {package['version']}", flush=True)
        with urlopen(package["url"], timeout=120) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != package["sha256"]:
            raise ValueError(f"{name} 下载校验失败，未安装。请重新运行准备命令。")
        unpacked = staging / "unpacked"
        unpacked.mkdir()
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    extracted = Path(bundle.extract(member, unpacked))
                    # ZipFile does not restore Unix executable bits; Chrome has helper programs.
                    if not member.is_dir() and (member.external_attr >> 16) & 0o111:
                        extracted.chmod(0o755)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(unpacked, filter="data")
        source = unpacked / package["root"]
        executable = source / package["bin"] / package["executable"]
        if not executable.is_file():
            raise ValueError(f"{name} 运行包缺少程序：{package['executable']}")
        executable.chmod(0o755)
        # Windows can refuse a directory rename; shutil.move then uses a copy.
        assert source.resolve().is_relative_to(staging.resolve())
        assert destination.resolve().is_relative_to((root / ".tools/runtime").resolve())
        shutil.move(source, destination)


def install_linux_packages(names: list[str]) -> None:
    """Use the distribution's package manager for system components."""
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    if shutil.which("apt-get"):
        commands = [["apt-get", "update"], ["apt-get", "install", "-y", *names]]
    elif shutil.which("dnf"):
        commands = [["dnf", "install", "-y", *names]]
    else:
        raise ValueError(f"请先用系统包管理器安装 {'、'.join(names)}，再重新运行准备命令。")
    print(f"通过系统包管理器安装 {'、'.join(names)}；可能需要输入 sudo 密码。", flush=True)
    for command in commands:
        subprocess.run(prefix + command, check=True)


def prepare(root: Path) -> None:
    activate(root)
    for name, package in packages(root).items():
        if name == "chrome":
            ready = (tool_directory(root, name) / package["bin"] / package["executable"]).is_file()
        elif name == "node":
            ready = supported_node() and shutil.which("npm.cmd" if os.name == "nt" else "npm")
        else:
            ready = shutil.which(name) or name == "fd" and shutil.which("fdfind")
        if not ready:
            install_package(root, name, package)
            activate(root)
    if os.name != "nt" and shutil.which("git") is None:
        install_linux_packages(["git"])


if __name__ == "__main__":
    prepare(ROOT)
