"""Deliver the Windows task runtime independently of the file service."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from scripts import agentfs_runtime as delivery

REQUIRED = {"redpanda-command.exe", "redpanda-hook.dll", "licenses/DETOURS-LICENSE.md",
            "licenses/DETOURS-SOURCE.md", "licenses/LLVM-LICENSE.txt"}
TARGET = "windows-x86_64"


def verify(directory: Path, record: dict) -> None:
    if record["target"] != TARGET or record["profile"] not in {"debug", "release"}:
        raise ValueError("Windows execution build target or profile is invalid")
    delivery.verify(directory, record, TARGET, required=REQUIRED)


def package(root: Path, output: Path) -> Path:
    source = root / "redpanda/execution/bin"
    record = json.loads((source / "BUILD.json").read_text(encoding="utf-8"))
    if record["profile"] != "release" or record["target"] != TARGET:
        raise ValueError("Windows execution runtime requires a release x64 build")
    verify(source, record)
    record["package"] = {"version": delivery.release_config(root)["version"], "platform": TARGET}
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "redpanda-execution-windows-x86_64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("BUILD.json", json.dumps(record, indent=2))
        for name in sorted(record["files"]):
            bundle.write(source / name, name)
    archive.with_suffix(".zip.sha256").write_text(f"{delivery.digest(archive)}  {archive.name}\n", encoding="ascii", newline="\n")
    return archive


def install(root: Path, *, replace: bool = False) -> Path:
    destination = root / "redpanda/execution/bin"
    executable = destination / "redpanda-command.exe"
    config = delivery.release_config(root)
    identity = {"version": config["version"], "platform": TARGET}
    if (destination / "BUILD.json").is_file() and not replace:
        record = json.loads((destination / "BUILD.json").read_text(encoding="utf-8"))
        try:
            verify(destination, record)
        except ValueError as error:
            raise ValueError(f"{error}；请先检查本地修改，确认替换时使用 install --replace。") from error
        if "package" not in record or record["package"] == identity:
            return executable
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".execution-", dir=destination.parent))
    backup = staging / "previous"
    try:
        archive = staging / "redpanda-execution-windows-x86_64.zip"
        base = f"https://github.com/{config['repository']}/releases/download/agentFS-v{config['version']}"
        delivery.download(f"{base}/{archive.name}", archive)
        checksum = staging / "checksum"
        delivery.download(f"{base}/{archive.name}.sha256", checksum)
        if checksum.read_text(encoding="ascii").split() != [delivery.digest(archive), archive.name]:
            raise ValueError("Windows execution package checksum mismatch")
        unpacked = staging / "bin"
        unpacked.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            record = json.loads(bundle.read("BUILD.json"))
            if record["package"] != identity:
                raise ValueError("Windows execution package version mismatch")
            names = [entry.filename for entry in bundle.infolist()]
            expected = delivery.manifest_files(record, TARGET, required=REQUIRED) | {"BUILD.json"}
            if len(names) != len({name.casefold() for name in names}) or set(names) != expected:
                raise ValueError("Windows execution archive differs from manifest")
            bundle.extractall(unpacked)
        verify(unpacked, record)
        if destination.exists():
            destination.rename(backup)
        try:
            unpacked.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            assert backup.resolve().is_relative_to(staging.resolve())
            shutil.rmtree(backup)
    finally:
        if not backup.exists():
            assert staging.resolve().is_relative_to(destination.parent.resolve())
            shutil.rmtree(staging)
    return executable
