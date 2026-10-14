import ctypes
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

root=Path(__file__).resolve().parent
repo=Path.cwd()
toolchain=repo/".tools/llvm-mingw-20261006-ucrt-x86_64/bin"
def output(argv):
    return subprocess.check_output(list(map(str,argv)),text=True,encoding="utf-8").strip()
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
versions={
    "clang":output([toolchain/"x86_64-w64-mingw32-clang++.exe","--version"]),
    "git":output([shutil.which("git"),"--version"]),
    "node":output([shutil.which("node"),"--version"]),
    "powershell_5":output([shutil.which("powershell"),"-NoLogo","-NoProfile","-NonInteractive","-Command","$PSVersionTable.PSVersion.ToString()"]),
    "powershell_7":output([shutil.which("pwsh"),"-NoLogo","-NoProfile","-NonInteractive","-Command","$PSVersionTable.PSVersion.ToString()"]),
    "winfsp":output([shutil.which("pwsh"),"-NoLogo","-NoProfile","-NonInteractive","-Command","(Get-Item redpanda/filesystem/bin/winfsp-x64.dll).VersionInfo.FileVersion"]),
}
names=["hook.cpp","hook.def","launcher.cpp","policy.h","build.ps1","cases.py","run_probe.py","git_control.py","limits_probe.py","benchmark.py","metadata.py","hook.dll","hook-attach-failure.dll","launcher.exe","launcher-no-policy.exe"]
image=output([toolchain/"llvm-objdump.exe","-p",root/"hook.dll"])
record={"recorded_at":datetime.now(timezone.utc).isoformat(),"elevated":bool(ctypes.windll.shell32.IsUserAnAdmin()),
    "detours_commit":output(["git","-C",root/"detours","rev-parse","HEAD"]),
    "versions":versions,"files_sha256":{name:sha(root/name) for name in names},
    "hook_imports":re.findall(r"DLL Name: (.+)",image),
    "native_build":json.loads((repo/"redpanda/filesystem/bin/BUILD.json").read_text()),
    "native_executable_sha256":sha(repo/"redpanda/filesystem/bin/redpanda-sandbox.exe")}
assert record["native_executable_sha256"]==record["native_build"]["files"]["redpanda-sandbox.exe"]
assert not record["elevated"]
(root/"metadata.json").write_text(json.dumps(record,indent=2),encoding="utf-8")
print(json.dumps({"elevated":record["elevated"],"imports":record["hook_imports"],"hashed_files":len(names)}))
