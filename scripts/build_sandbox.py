"""Build RED PANDA's native sandbox with the VFS sources in this repository."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import platform
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"


class MissingBuildDependencies(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class BuildEnvironment:
    variables: dict[str, str]
    rustup: str
    toolchain: str
    winfsp: Path | None
    llvm: Path | None


def prepare_environment(llvm_dir: Path | None = None, winfsp_dir: Path | None = None) -> BuildEnvironment:
    """检查本机依赖，只为构建子进程准备环境，不修改用户工具链设置。"""
    environment = dict(os.environ)
    errors = []
    channel = tomllib.loads((ROOT / "native/sandbox/rust-toolchain.toml").read_text(encoding="utf-8"))["toolchain"]["channel"]
    toolchain = channel + "-x86_64-pc-windows-gnullvm" if WINDOWS else channel
    rustup = shutil.which("rustup")
    if rustup is None:
        errors.append("缺少 rustup：请先安装 Rust 工具链管理器。")
    else:
        result = subprocess.run([rustup, "run", toolchain, "rustc", "--version"],
                                capture_output=True, text=True)
        if result.returncode:
            errors.append(f"Rust 工具链不可用：{result.stderr.strip()}\n"
                          f"  安装命令：rustup toolchain install {toolchain} --profile minimal --component rustfmt,clippy")

    winfsp = llvm = None
    if WINDOWS:
        winfsp = winfsp_dir or Path(os.environ["ProgramFiles(x86)"]) / "WinFsp"
        compiler = shutil.which("x86_64-w64-mingw32-clang.exe")
        llvm = llvm_dir or (Path(compiler).parent if compiler else None)
        for relative in ("inc/winfsp/winfsp.h", "lib/winfsp-x64.lib", "bin/winfsp-x64.dll", "License.txt"):
            if not (winfsp / relative).is_file():
                errors.append(f"WinFsp 缺少 {winfsp / relative}；请安装驱动及 Developer SDK，或使用 --winfsp-dir。")
        if llvm is None:
            errors.append("缺少 LLVM MinGW：请将 bin 目录加入 PATH，或使用 --llvm-dir 指定 bin 目录。")
        else:
            llvm = llvm.resolve()
            for name in ("x86_64-w64-mingw32-clang.exe", "x86_64-w64-mingw32-clang++.exe", "llvm-ar.exe", "libunwind.dll"):
                if not (llvm / name).is_file():
                    errors.append(f"LLVM MinGW 缺少 {llvm / name}。")
            if not (llvm.parent / "LICENSE.TXT").is_file():
                errors.append(f"LLVM MinGW 缺少许可证 {llvm.parent / 'LICENSE.TXT'}。")
            environment.update({
                "CARGO_TARGET_X86_64_PC_WINDOWS_GNULLVM_LINKER": str(llvm / "x86_64-w64-mingw32-clang.exe"),
                "CC_x86_64_pc_windows_gnullvm": str(llvm / "x86_64-w64-mingw32-clang.exe"),
                "AR_x86_64_pc_windows_gnullvm": str(llvm / "llvm-ar.exe"),
                "PATH": os.pathsep.join((str(llvm), str(winfsp / "bin"), environment.get("PATH", ""))),
            })
        environment["WINFSP_INCLUDE_DIR"] = str((winfsp / "inc").resolve())
        environment["WINFSP_LIB_DIR"] = str((winfsp / "lib").resolve())
    elif sys.platform == "linux":
        for command in ("cc", "fusermount3"):
            if shutil.which(command) is None:
                errors.append(f"缺少 {command}；请安装 C 构建工具与 fuse3。")
        if not os.access("/dev/fuse", os.R_OK | os.W_OK):
            errors.append("/dev/fuse 不存在或当前用户无法读写；请启用 FUSE 并检查设备权限。")
        if not os.access("/etc/mtab", os.R_OK):
            errors.append("/etc/mtab 不可读；fusermount3 需要它完成卸载。")
    else:
        errors.append("原生沙箱当前只支持 Windows x64 与 Linux。")
    if errors:
        raise MissingBuildDependencies(errors)
    environment["LIBSQLITE3_FLAGS"] = "-DSQLITE_DIRECT_OVERFLOW_READ=0"
    environment["RUSTUP_TOOLCHAIN"] = toolchain
    return BuildEnvironment(environment, rustup, toolchain, winfsp, llvm)


def source_digest(root: Path) -> str:
    files = []
    for directory, directories, names in os.walk(root):
        directories[:] = sorted(name for name in directories
                                if name not in {"target", "__pycache__", ".git"})
        for name in sorted(names):
            path = Path(directory) / name
            files.append((path.relative_to(root).as_posix(),
                          hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(sorted(files), ensure_ascii=False).encode("utf-8")).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="x86_64-pc-windows-gnullvm" if WINDOWS else
                        "x86_64-unknown-linux-gnu" if platform.machine().lower() in {"x86_64", "amd64"} else None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--llvm-dir", type=Path, help="Windows LLVM MinGW 的 bin 目录；缺省从 PATH 查找")
    parser.add_argument("--winfsp-dir", type=Path, help="Windows WinFsp 安装目录（包含 bin、inc、lib）")
    parser.add_argument("--check", action="store_true", help="只检查原生构建和运行依赖")
    args = parser.parse_args()
    windows = WINDOWS
    if not windows and (args.winfsp_dir or args.llvm_dir):
        parser.error("WinFsp and LLVM runtime options apply to Windows builds")
    if windows and args.target != "x86_64-pc-windows-gnullvm":
        parser.error("Windows builds require the x86_64-pc-windows-gnullvm target")
    try:
        prepared = prepare_environment(args.llvm_dir, args.winfsp_dir)
    except MissingBuildDependencies as error:
        parser.exit(1, "原生依赖未就绪：\n" + "\n".join(error.errors) + "\n")
    if args.check:
        print("原生构建与运行依赖检查通过。")
        return

    factory = ROOT / "native/vfs"
    tools = ROOT / ".tools"
    tools.mkdir(exist_ok=True)

    environment = prepared.variables
    target = Path(environment.get("CARGO_TARGET_DIR", tools / "sandbox/target")).resolve()
    environment["CARGO_TARGET_DIR"] = str(target)
    temporary = tools / "sandbox/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    environment["TMP"] = environment["TEMP"] = str(temporary)

    command = [prepared.rustup, "run", prepared.toolchain, "cargo", "build", "--locked"]
    if args.target:
        command.extend(["--target", args.target])
    if not args.debug:
        command.append("--release")
    if args.offline:
        command.append("--offline")
    subprocess.run(command, cwd=ROOT / "native/sandbox", env=environment, check=True)

    output = ROOT / "redpanda/filesystem/bin"
    output.mkdir(parents=True, exist_ok=True)
    profile = "debug" if args.debug else "release"
    artifact_dir = target / profile if not args.target else target / args.target / profile
    binary_name = "redpanda-sandbox.exe" if windows else "redpanda-sandbox"
    shutil.copy2(artifact_dir / binary_name, output)
    licenses = output / "licenses"
    licenses.mkdir(exist_ok=True)
    if windows:
        winfsp = prepared.winfsp
        shutil.copy2(winfsp / "bin/winfsp-x64.dll", output)
        shutil.copy2(winfsp / "License.txt", licenses / "WINFSP-LICENSE.txt")
    shutil.copy2(factory / "README.md", licenses / "FACTORY-README.md")
    shutil.copy2(factory / "SOURCE.md", licenses / "FACTORY-SOURCE.md")
    for path in (factory / "licenses").glob("*"):
        shutil.copy2(path, licenses / path.name)
    if prepared.llvm is not None:
        runtime = prepared.llvm
        shutil.copy2(runtime / "libunwind.dll", output)
        shutil.copy2(runtime.parent / "LICENSE.TXT", licenses / "LLVM-LICENSE.txt")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts.build_execution import build
        build(ROOT, prepared.llvm, profile)

    files = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(output.rglob("*")) if path.is_file() and path.name != "BUILD.json"}
    record = {"target": args.target or "host", "profile": profile,
              "sqlite_c_flags": environment["LIBSQLITE3_FLAGS"],
              "source_sha256": {"native/vfs": source_digest(factory),
                                "native/sandbox": source_digest(ROOT / "native/sandbox"),
                                ".cargo/config.toml": hashlib.sha256((ROOT / ".cargo/config.toml").read_bytes()).hexdigest(),
                                "scripts/build_sandbox.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
              "files": files}
    (output / "BUILD.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output / binary_name)


if __name__ == "__main__":
    main()
