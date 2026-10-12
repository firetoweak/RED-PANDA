import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_sandbox


@pytest.mark.parametrize("explicit_paths", [True, False])
def test_windows_build_environment_uses_explicit_host_and_keeps_process_environment(tmp_path, monkeypatch, explicit_paths):
    sdk = tmp_path / "WinFsp"
    llvm = tmp_path / "LLVM MinGW" / "bin"
    files = [sdk / name for name in ("inc/winfsp/winfsp.h", "lib/winfsp-x64.lib",
                                    "bin/winfsp-x64.dll", "License.txt")]
    files += [llvm / name for name in ("x86_64-w64-mingw32-clang.exe", "llvm-ar.exe",
                                      "libclang.dll", "libunwind.dll")]
    files.append(llvm.parent / "LICENSE.TXT")
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(build_sandbox, "WINDOWS", True)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.setattr(build_sandbox.shutil, "which", lambda command: "rustup.exe" if command == "rustup"
                        else str(llvm / "x86_64-w64-mingw32-clang.exe"))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_sandbox.subprocess, "run", run)
    before = dict(os.environ)

    result = build_sandbox.prepare_environment(llvm, sdk) if explicit_paths else build_sandbox.prepare_environment()

    assert dict(os.environ) == before
    assert result.toolchain == "nightly-2026-08-07-x86_64-pc-windows-gnullvm"
    assert calls == [["rustup.exe", "run", result.toolchain, "rustc", "--version"]]
    assert result.variables["CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER"] == str(llvm / "x86_64-w64-mingw32-clang.exe")
    assert result.variables["AR_x86_64_pc_windows_gnullvm"] == str(llvm / "llvm-ar.exe")
    assert result.variables["WINFSP_INCLUDE_DIR"] == str(sdk / "inc")
    assert result.variables["PATH"].split(os.pathsep)[:2] == [str(llvm), str(sdk / "bin")]
    assert result.llvm == llvm


def test_missing_native_dependencies_are_reported_together(tmp_path, monkeypatch):
    monkeypatch.setattr(build_sandbox, "WINDOWS", True)
    monkeypatch.setattr(build_sandbox.shutil, "which", lambda _: None)
    with pytest.raises(build_sandbox.MissingBuildDependencies) as error:
        build_sandbox.prepare_environment(tmp_path / "missing-llvm", tmp_path / "missing-sdk")
    message = str(error.value)
    assert "rustup" in message
    assert "winfsp.h" in message
    assert "winfsp-x64.lib" in message
    assert "libunwind.dll" in message
    assert "LICENSE.TXT" in message


def test_linux_checks_mount_permissions_alongside_compilation_tools(monkeypatch):
    monkeypatch.setattr(build_sandbox, "WINDOWS", False)
    monkeypatch.setattr(build_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(build_sandbox.shutil, "which", lambda _: None)
    monkeypatch.setattr(build_sandbox.os, "access", lambda *args: False)
    with pytest.raises(build_sandbox.MissingBuildDependencies) as error:
        build_sandbox.prepare_environment()
    message = str(error.value)
    assert "cc" in message
    assert "fusermount3" in message
    assert "/dev/fuse" in message
    assert "/etc/mtab" in message


def test_build_packages_detected_runtime_and_uses_prepared_toolchain(tmp_path, monkeypatch):
    sdk, llvm = tmp_path / "sdk", tmp_path / "llvm/bin"
    for path in (sdk / "bin/winfsp-x64.dll", sdk / "License.txt", llvm / "libunwind.dll",
                 llvm.parent / "LICENSE.TXT", tmp_path / "native/vfs/README.md",
                 tmp_path / "native/vfs/SOURCE.md", tmp_path / ".cargo/config.toml"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test artifact", encoding="utf-8")
    (tmp_path / "native/sandbox").mkdir(parents=True)
    prepared = build_sandbox.BuildEnvironment({"LIBSQLITE3_FLAGS": "-DSQLITE_DIRECT_OVERFLOW_READ=0"},
                                              "rustup", "explicit-host", sdk, llvm)
    monkeypatch.setattr(build_sandbox, "ROOT", tmp_path)
    monkeypatch.setattr(build_sandbox, "WINDOWS", True)
    monkeypatch.setattr(build_sandbox, "prepare_environment", lambda *args: prepared)
    monkeypatch.setattr(build_sandbox.sys, "argv", ["build_sandbox.py"])
    calls = []

    def compile_native(command, **kwargs):
        calls.append(command)
        artifact = Path(kwargs["env"]["CARGO_TARGET_DIR"]) / "x86_64-pc-windows-gnullvm/release/redpanda-sandbox.exe"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"native artifact")

    monkeypatch.setattr(build_sandbox.subprocess, "run", compile_native)
    build_sandbox.main()
    assert calls == [["rustup", "run", "explicit-host", "cargo", "build", "--locked",
                      "--target", "x86_64-pc-windows-gnullvm", "--release"]]
    output = tmp_path / "redpanda/sandbox/bin"
    assert (output / "redpanda-sandbox.exe").read_bytes() == b"native artifact"
    assert (output / "libunwind.dll").is_file()
    assert (output / "winfsp-x64.dll").is_file()
    assert (output / "licenses/LLVM-LICENSE.txt").is_file()
    assert (output / "BUILD.json").is_file()
