"""Python 引导完成后，准备运行依赖、预编译沙箱与模型配置。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"


def check_dependencies() -> list[str]:
    errors = []
    for label, commands in (
        ("Git", ("git",)), ("ripgrep", ("rg",)), ("fd", ("fd", "fdfind")),
        ("Node.js", ("node",)), ("npm", ("npm.cmd",) if WINDOWS else ("npm",)),
    ):
        if not any(shutil.which(command) for command in commands):
            errors.append(f"缺少 {label}：请先安装并加入 PATH。")
    if shutil.which("node"):
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True)
        version = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
        if version is None or not (int(version[1]) == 24 or int(version[1]) == 22 and int(version[2]) >= 13):
            errors.append(f"Node.js 版本不支持：{result.stdout.strip()}；请使用 22.x（至少 22.13）或 24.x。")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-configure", action="store_true", help="只初始化配置文件，不启动模型向导")
    parser.add_argument("--check", action="store_true", help="只检查项目所需系统工具，不安装项目依赖")
    args = parser.parse_args(argv)
    errors = check_dependencies()
    if errors:
        parser.exit(1, "系统依赖未就绪：\n" + "\n".join(f"- {error}" for error in errors)
                    + "\n安装方法见 README.md。\n")
    if args.check:
        print("系统依赖检查通过。")
        return 0
    uv = ROOT / ".tools/uv" / ("uv.exe" if WINDOWS else "uv")
    print("[1/4] 安装 Python 依赖", flush=True)
    subprocess.run([str(uv), "pip", "install", "--python", sys.executable, "pip", "-r", "requirements.txt"],
                   cwd=ROOT, check=True)
    print("[2/4] 检查并补齐前端依赖", flush=True)
    npm = shutil.which("npm.cmd" if WINDOWS else "npm")
    subprocess.run([npm, "--prefix", "web", "install"], cwd=ROOT, check=True)
    print("[3/4] 准备沙箱运行包与平台运行组件", flush=True)
    subprocess.run([sys.executable, "-m", "scripts.sandbox_runtime", "install", "--with-runtime"],
                   cwd=ROOT, check=True)
    print("[4/4] 初始化模型配置", flush=True)
    initialize = [sys.executable, "-m", "redpanda.initialize"]
    if not args.no_configure:
        initialize.append("--if-needed")
    subprocess.run(initialize, cwd=ROOT, check=True)
    print("运行环境准备完成。使用 scripts/dev.ps1 或 scripts/dev.sh 启动。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        print(f"准备未完成（命令退出码 {error.returncode}）。请处理上方错误后重新执行。", file=sys.stderr)
        raise SystemExit(error.returncode)
