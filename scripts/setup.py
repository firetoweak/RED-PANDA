"""Python 引导完成后，准备应用工具、网页与原生文件服务。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

from scripts import runtime_tools

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
    if shutil.which("node") and not runtime_tools.supported_node():
        errors.append("Node.js 版本不支持；请使用 22.x（至少 22.13）或 24.x，或重新运行准备命令自动安装。")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configure", action="store_true", help="安装后在终端配置模型；默认在网页中配置")
    parser.add_argument("--check", action="store_true", help="只检查项目所需系统工具，不安装项目依赖")
    args = parser.parse_args(argv)
    if args.check:
        runtime_tools.activate(ROOT)
    else:
        print("[1/5] 准备文件服务、任务命令组件与平台运行组件", flush=True)
        subprocess.run([sys.executable, "-m", "scripts.agentfs_runtime", "install", "--with-runtime"],
                       cwd=ROOT, check=True)
        print("[2/5] 准备应用命令行工具", flush=True)
        runtime_tools.prepare(ROOT)
    errors = check_dependencies()
    if errors:
        parser.exit(1, "系统依赖未就绪：\n" + "\n".join(f"- {error}" for error in errors)
                    + "\n重新运行准备命令可自动补齐应用工具。\n")
    if args.check:
        print("系统依赖检查通过。")
        return 0
    uv = ROOT / ".tools/uv" / ("uv.exe" if WINDOWS else "uv")
    print("[3/5] 安装 Python 依赖", flush=True)
    # uv-created environments do not contain pip; keep it available for developer commands.
    subprocess.run([str(uv), "pip", "install", "--python", sys.executable, "pip"], cwd=ROOT, check=True)
    subprocess.run([str(uv), "pip", "install", "--python", sys.executable, "-r", "requirements.lock", "--require-hashes"],
                   cwd=ROOT, check=True)
    print("[4/5] 安装前端依赖并构建网页", flush=True)
    npm = shutil.which("npm.cmd" if WINDOWS else "npm")
    subprocess.run([npm, "--prefix", "web", "ci"], cwd=ROOT, check=True)
    subprocess.run([npm, "--prefix", "web", "run", "build"], cwd=ROOT, check=True)
    print("[5/5] 初始化配置", flush=True)
    initialize = [sys.executable, "-m", "redpanda.initialize"]
    if args.configure:
        initialize.append("--configure")
    subprocess.run(initialize, cwd=ROOT, check=True)
    print("运行环境准备完成。使用 scripts/start.ps1 或 scripts/start.sh 启动，在网页侧栏配置模型。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        print(f"准备未完成（命令退出码 {error.returncode}）。请处理上方错误后重新执行。", file=sys.stderr)
        raise SystemExit(error.returncode)
    except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"准备未完成：{error}", file=sys.stderr)
        raise SystemExit(1)
