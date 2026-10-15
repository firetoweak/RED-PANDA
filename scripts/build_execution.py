"""Build the Windows x64 task launcher and hook from pinned vendored sources."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def build(root: Path, llvm: Path, profile: str = "release") -> Path:
    source = root / "native/execution"
    vendor = source / "vendor/detours"
    objects = root / ".tools/execution/objects"
    objects.mkdir(parents=True, exist_ok=True)
    output = root / "redpanda/execution/bin"
    output.mkdir(parents=True, exist_ok=True)
    compiler = llvm / "x86_64-w64-mingw32-clang++.exe"
    flags = ["-std=c++17", "-O2", "-DDETOUR_DEBUG=0", "-D_WIN32_WINNT=0x0A00",
             "-DWIN32_LEAN_AND_MEAN", "-Wno-unknown-pragmas", "-Wno-macro-redefined",
             "-Wno-ignored-attributes", "-I", str(vendor / "src")]
    if profile == "debug":
        flags.append("-g")
    library_objects = []
    for name in ("detours", "modules", "disasm", "image", "creatwth", "disolx86", "disolx64", "disolia64", "disolarm", "disolarm64"):
        obj = objects / f"{name}.o"
        subprocess.run([str(compiler), *flags, "-c", str(vendor / f"src/{name}.cpp"), "-o", str(obj)], check=True)
        library_objects.append(str(obj))
    library = objects.parent / "detours.a"
    subprocess.run([str(llvm / "llvm-ar.exe"), "rcs", str(library), *library_objects], check=True)
    for name, options, sources in (
        ("redpanda-hook.dll", ["-shared"], ["hook.cpp", "hook.def"]),
        ("redpanda-command.exe", ["-municode"], ["launcher.cpp"]),
    ):
        subprocess.run([str(compiler), *flags, "-static", *options,
                        *(str(source / name) for name in sources), str(library), "-o", str(output / name)], check=True)
    licenses = output / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(vendor / "LICENSE.md", licenses / "DETOURS-LICENSE.md")
    shutil.copyfile(vendor / "SOURCE.md", licenses / "DETOURS-SOURCE.md")
    shutil.copyfile(llvm.parent / "LICENSE.TXT", licenses / "LLVM-LICENSE.txt")
    names = ["redpanda-hook.dll", "redpanda-command.exe", "licenses/DETOURS-LICENSE.md", "licenses/DETOURS-SOURCE.md", "licenses/LLVM-LICENSE.txt"]
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    record = {"target": "windows-x86_64", "profile": profile,
              "source_sha256": {p.relative_to(root).as_posix(): digest(p) for p in sorted(source.rglob("*")) if p.is_file()},
              "files": {name: digest(output / name) for name in names}}
    record["source_sha256"]["scripts/build_execution.py"] = digest(Path(__file__))
    (output / "BUILD.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llvm-dir", required=True, type=Path)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[1], args.llvm_dir.resolve(), "debug" if args.debug else "release"))


if __name__ == "__main__":
    main()
