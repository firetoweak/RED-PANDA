"""Build RED PANDA's native sandbox with the VFS sources in this repository."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


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
    parser.add_argument("--target", default="x86_64-pc-windows-msvc" if os.name == "nt" else None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--winfsp-include", type=Path)
    parser.add_argument("--winfsp-lib", type=Path)
    parser.add_argument("--runtime-dir", type=Path, help="LLVM runtime directory for the GNU LLVM target")
    args = parser.parse_args()
    windows = os.name == "nt"
    if not windows and (args.winfsp_include or args.winfsp_lib or args.runtime_dir):
        parser.error("WinFsp and LLVM runtime options apply to Windows builds")

    factory = ROOT / "native/vfs"
    tools = ROOT / ".tools"
    tools.mkdir(exist_ok=True)

    environment = dict(os.environ)
    if windows:
        winfsp = Path(os.environ["ProgramFiles(x86)"]) / "WinFsp"
        include = args.winfsp_include or Path(environment.get("WINFSP_INCLUDE_DIR", winfsp / "inc"))
        library = args.winfsp_lib or Path(environment.get("WINFSP_LIB_DIR", winfsp / "lib"))
        environment["WINFSP_INCLUDE_DIR"] = str(include.resolve(strict=True))
        environment["WINFSP_LIB_DIR"] = str(library.resolve(strict=True))
    target = Path(environment.get("CARGO_TARGET_DIR", tools / "sandbox/target")).resolve()
    environment["CARGO_TARGET_DIR"] = str(target)
    temporary = tools / "sandbox/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    environment["TMP"] = environment["TEMP"] = str(temporary)

    command = ["cargo", "build", "--locked"]
    if args.target:
        command.extend(["--target", args.target])
    if not args.debug:
        command.append("--release")
    if args.offline:
        command.append("--offline")
    subprocess.run(command, cwd=ROOT / "native/sandbox", env=environment, check=True)

    output = ROOT / "redpanda/sandbox/bin"
    output.mkdir(parents=True, exist_ok=True)
    profile = "debug" if args.debug else "release"
    artifact_dir = target / profile if not args.target else target / args.target / profile
    binary_name = "redpanda-sandbox.exe" if windows else "redpanda-sandbox"
    shutil.copy2(artifact_dir / binary_name, output)
    licenses = output / "licenses"
    licenses.mkdir(exist_ok=True)
    if windows:
        shutil.copy2(winfsp / "bin/winfsp-x64.dll", output)
        shutil.copy2(winfsp / "License.txt", licenses / "WINFSP-LICENSE.txt")
    shutil.copy2(factory / "README.md", licenses / "FACTORY-README.md")
    shutil.copy2(factory / "SOURCE.md", licenses / "FACTORY-SOURCE.md")
    for path in (factory / "licenses").glob("*"):
        shutil.copy2(path, licenses / path.name)
    if args.runtime_dir is not None:
        runtime = args.runtime_dir.resolve(strict=True)
        shutil.copy2(runtime / "libunwind.dll", output)
        shutil.copy2(runtime.parent / "LICENSE.TXT", licenses / "LLVM-LICENSE.txt")

    files = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(output.rglob("*")) if path.is_file() and path.name != "BUILD.json"}
    record = {"target": args.target or "host", "profile": profile,
              "source_sha256": {"native/vfs": source_digest(factory),
                                "native/sandbox": source_digest(ROOT / "native/sandbox"),
                                "scripts/build_sandbox.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
              "files": files}
    (output / "BUILD.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output / binary_name)


if __name__ == "__main__":
    main()
