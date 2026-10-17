"""Compile the gate fault-injection runtime for local tests and CI artifacts."""
from pathlib import Path
import subprocess


def build(root: Path, output: Path) -> Path:
    compiler = root / ".tools/llvm-mingw-20261006-ucrt-x86_64/bin/x86_64-w64-mingw32-clang++.exe"
    library = root / ".tools/execution/detours.a"
    assert compiler.is_file() and library.is_file(), "Build Windows execution components before process verification"
    source = root / "native/execution"
    output.mkdir(parents=True, exist_ok=True)
    common = [str(compiler), "-std=c++17", "-O2", "-static", "-DDETOUR_DEBUG=0", "-D_WIN32_WINNT=0x0A00",
              "-I", str(source), "-I", str(source / "vendor/detours/src")]
    subprocess.run([*common, "-shared", str(root / "tests/fixtures/windows_gate_hook.cpp"),
                    str(source / "hook.def"), str(library), "-o", str(output / "redpanda-hook.dll")],
                   check=True, capture_output=True)
    launcher = output / "redpanda-command.exe"
    subprocess.run([*common, "-municode", str(root / "tests/fixtures/windows_gate_launcher.cpp"),
                    str(library), "-o", str(launcher)], check=True, capture_output=True)
    return launcher


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    print(build(root, root / ".tools/test-fixtures/windows-gate"))
