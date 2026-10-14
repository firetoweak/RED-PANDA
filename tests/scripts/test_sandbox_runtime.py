"""运行包交付契约：可重复安装，保护本地构建，坏包不破坏已有程序。"""
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from scripts import sandbox_runtime as runtime


def configure(root, version="0.4.1"):
    config = root / "native/sandbox/release.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"version": version, "repository": "example/project"}), encoding="utf-8")


def make_build(root, target):
    configure(root)
    directory = root / "redpanda/filesystem/bin"
    directory.mkdir(parents=True)
    names = {runtime.PLATFORMS[target], "licenses/SOURCE.md"}
    if target.startswith("windows"):
        names.update({"winfsp-x64.dll", "libunwind.dll", "licenses/WINFSP-LICENSE.txt", "licenses/LLVM-LICENSE.txt"})
    for name in names:
        path = directory / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(f"compiled {name}".encode())
    record = {"target": "x86_64-pc-windows-gnullvm" if target.startswith("windows") else "x86_64-unknown-linux-gnu",
              "profile": "release", "source_sha256": {"native/sandbox": "source"},
              "files": {name: runtime.digest(directory / name) for name in names}}
    (directory / "BUILD.json").write_text(json.dumps(record), encoding="utf-8")
    return directory


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    producer, consumer = tmp_path / "producer", tmp_path / "consumer"
    make_build(producer, "windows-x86_64")
    configure(consumer)
    archive = runtime.package(producer, tmp_path / "release", "windows-x86_64")
    monkeypatch.setattr(runtime, "host_platform", lambda: "windows-x86_64")
    monkeypatch.delenv("REDPANDA_FILESYSTEM_EXECUTABLE", raising=False)
    requests = []

    def download(url, destination):
        requests.append(url)
        shutil.copyfile(archive.with_name(url.rsplit("/", 1)[1]), destination)

    monkeypatch.setattr(runtime, "download", download)
    return producer, consumer, archive, requests


def rewrite_archive(archive, change):
    with zipfile.ZipFile(archive) as bundle:
        files = {name: bundle.read(name) for name in bundle.namelist()}
    change(files)
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    archive.with_suffix(".zip.sha256").write_text(f"{runtime.digest(archive)}  {archive.name}\n", encoding="ascii")


@pytest.mark.parametrize("target", runtime.PLATFORMS)
def test_package_has_runtime_files_and_leaves_local_build_unmarked(tmp_path, target):
    source = make_build(tmp_path, target)
    original = (source / "BUILD.json").read_bytes()
    archive = runtime.package(tmp_path, tmp_path / "dist", target)
    assert archive.with_suffix(".zip.sha256").read_bytes() == (
        f"{runtime.digest(archive)}  {archive.name}\n".encode("ascii")
    )
    with zipfile.ZipFile(archive) as bundle:
        record = json.loads(bundle.read("BUILD.json"))
        assert set(bundle.namelist()) == set(record["files"]) | {"BUILD.json"}
        assert record["package"] == {"version": "0.4.1", "platform": target}
    assert (source / "BUILD.json").read_bytes() == original


def test_fresh_install_is_pinned_and_second_run_is_offline(delivery, monkeypatch):
    _, root, _, requests = delivery
    executable = runtime.install(root)
    assert executable.read_bytes() == b"compiled redpanda-sandbox.exe"
    assert len(requests) == 2
    assert all("/sandbox-v0.4.1/" in url for url in requests)
    monkeypatch.setattr(runtime, "download", lambda *args: pytest.fail("already installed"))
    assert runtime.install(root) == executable


def test_reinstall_preserves_local_build_and_explicit_override(delivery, monkeypatch, tmp_path):
    _, root, _, requests = delivery
    local = make_build(root, "windows-x86_64") / "redpanda-sandbox.exe"
    assert runtime.install(root) == local
    custom = tmp_path / "custom.exe"
    custom.write_bytes(b"custom")
    monkeypatch.setenv("REDPANDA_FILESYSTEM_EXECUTABLE", str(custom))
    assert runtime.install(root) == custom
    assert requests == []


def test_changed_managed_binary_is_protected_unless_replacement_is_explicit(delivery):
    _, root, _, requests = delivery
    executable = runtime.install(root)
    executable.write_bytes(b"local edit")
    with pytest.raises(ValueError, match="--replace"):
        runtime.install(root)
    assert len(requests) == 2
    assert executable.read_bytes() == b"local edit"
    runtime.install(root, replace=True)
    assert executable.read_bytes() == b"compiled redpanda-sandbox.exe"


def test_pinned_version_change_updates_only_managed_package(delivery):
    producer, root, archive, requests = delivery
    executable = runtime.install(root)
    configure(producer, "0.4.2")
    configure(root, "0.4.2")
    source = producer / "redpanda/filesystem/bin"
    (source / "redpanda-sandbox.exe").write_bytes(b"new release")
    record = json.loads((source / "BUILD.json").read_text())
    record["files"]["redpanda-sandbox.exe"] = runtime.digest(source / "redpanda-sandbox.exe")
    (source / "BUILD.json").write_text(json.dumps(record))
    runtime.package(producer, archive.parent, "windows-x86_64")
    runtime.install(root)
    assert executable.read_bytes() == b"new release"
    assert "/sandbox-v0.4.2/" in requests[-1]


@pytest.mark.parametrize("corruption", ["archive_hash", "file_hash", "version", "platform", "extra_path", "manifest_path"])
def test_bad_delivery_never_replaces_existing_program(delivery, corruption):
    _, root, archive, _ = delivery
    original = make_build(root, "windows-x86_64") / "redpanda-sandbox.exe"
    before = original.read_bytes()
    if corruption == "archive_hash":
        archive.with_suffix(".zip.sha256").write_text("0" * 64 + f"  {archive.name}\n")
    else:
        def corrupt(files):
            if corruption == "file_hash":
                files["redpanda-sandbox.exe"] = b"tampered"
            elif corruption == "extra_path":
                files["../escaped"] = b"escaped"
            else:
                record = json.loads(files["BUILD.json"])
                if corruption == "manifest_path":
                    record["files"]["../escaped"] = "0" * 64
                else:
                    record["package"][corruption] = "wrong"
                files["BUILD.json"] = json.dumps(record).encode()
        rewrite_archive(archive, corrupt)
    with pytest.raises(ValueError):
        runtime.install(root, replace=True)
    assert original.read_bytes() == before
    assert not (root / "redpanda/filesystem/escaped").exists()


def test_failed_directory_swap_restores_previous_program(delivery, monkeypatch):
    _, root, _, _ = delivery
    original = make_build(root, "windows-x86_64") / "redpanda-sandbox.exe"
    before = original.read_bytes()
    rename = Path.rename

    def fail_install(path, destination):
        if path.name == "bin" and path.parent.name.startswith(".sandbox-"):
            raise PermissionError("program in use")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_install)
    with pytest.raises(PermissionError):
        runtime.install(root, replace=True)
    assert original.read_bytes() == before


def test_download_failure_keeps_previous_program(delivery, monkeypatch):
    _, root, _, _ = delivery
    original = make_build(root, "windows-x86_64") / "redpanda-sandbox.exe"
    before = original.read_bytes()
    def unavailable(*args):
        raise OSError("network unavailable")
    monkeypatch.setattr(runtime, "download", unavailable)
    with pytest.raises(OSError, match="network unavailable"):
        runtime.install(root, replace=True)
    assert original.read_bytes() == before


@pytest.mark.parametrize("system,machine", [("Darwin", "x86_64"), ("Linux", "aarch64"), ("Windows", "ARM64")])
def test_unsupported_platform_stops_before_download(monkeypatch, system, machine):
    monkeypatch.setattr(runtime.platform, "system", lambda: system)
    monkeypatch.setattr(runtime.platform, "machine", lambda: machine)
    with pytest.raises(ValueError, match="x64"):
        runtime.host_platform()


def test_windows_platform_install_does_not_request_development_sdk(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(runtime.subprocess, "run", lambda command, **kw: calls.append((command, kw)))
    runtime.ensure_platform_runtime(tmp_path)
    assert calls == [(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                      str(tmp_path / "scripts/install_winfsp.ps1")], {"check": True})]


@pytest.mark.parametrize("libc,version", [("glibc", "2.31"), ("musl", "1.2"), ("", "")])
def test_linux_package_requires_supported_libc(monkeypatch, libc, version):
    monkeypatch.setattr(runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(runtime.platform, "libc_ver", lambda: (libc, version))
    with pytest.raises(ValueError, match="glibc 2.35"):
        runtime.host_platform()


def test_explicit_replacement_can_repair_broken_metadata(delivery):
    _, root, _, _ = delivery
    executable = runtime.install(root)
    (executable.parent / "BUILD.json").write_text("broken metadata")
    assert runtime.install(root, replace=True) == executable


def test_unavailable_package_never_starts_driver_install(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ValueError("not published")
    monkeypatch.setattr(runtime, "install", unavailable)
    monkeypatch.setattr(runtime, "ensure_platform_runtime", lambda *args: pytest.fail("must not install driver"))
    with pytest.raises(SystemExit) as error:
        runtime.main(["install", "--with-runtime"])
    assert error.value.code == 1
