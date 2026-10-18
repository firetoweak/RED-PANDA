"""本地与 CI 共用的验证入口；--clean 在临时 checkout 中验证已提交的 HEAD。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def verification_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("REDPANDA_")
                   and key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH", "PYTHONHOME"}}
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return environment


def run(command, *, cwd: Path | None = None):
    return subprocess.run(command, cwd=cwd or ROOT, env=verification_environment(), check=True)


def clean_checkout(targets: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="redpanda-ci-") as temporary:
        checkout = Path(temporary).resolve() / "src"
        run(["git", "worktree", "add", "--detach", str(checkout), "HEAD"])
        try:
            python = checkout / "redpanda-env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            run([sys.executable, "-m", "venv", str(python.parent.parent)], cwd=checkout)
            run([str(python), "scripts/ci.py", *targets], cwd=checkout)
        finally:
            # Only the checkout created inside our own temporary directory is removed.
            assert checkout.resolve().is_relative_to(Path(temporary).resolve())
            run(["git", "worktree", "remove", "--force", str(checkout)])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="*", choices=("python", "web", "web-test", "web-build", "process"))
    parser.add_argument("--clean", action="store_true",
                        help="在独立虚拟环境与干净 HEAD 中验证 python/web；不携带本机产物")
    parser.add_argument("--process-group", choices=("compact", "supervisor", "execution", "integration"),
                        help="只验证指定 process 测试组；不指定时验证全部 process 用例")
    parser.add_argument("--workers", type=int, default=1,
                        help="process 组内并行 worker 数；默认串行")
    args = parser.parse_args(argv)
    targets = args.targets or ["python", "web"]
    if args.process_group and "process" not in targets:
        parser.error("--process-group 需要 process 验证目标")
    if args.workers < 1 or (args.workers != 1 and "process" not in targets):
        parser.error("--workers 必须为正整数，且只用于 process 验证目标")
    expected_python = (ROOT / ".python-version").read_text().strip()
    if f"{sys.version_info.major}.{sys.version_info.minor}" != expected_python:
        parser.error(f"请使用 Python {expected_python}")
    if args.clean:
        if "process" in targets:
            parser.error("--clean 只验证 python/web；process 在当前 checkout 中准备应用命令行工具")
        clean_checkout(targets)
        return
    # Also applies before collection and to all test child processes.
    environment = verification_environment()
    os.environ.clear()
    os.environ.update(environment)
    needs_web = any(target in {"web", "web-test", "web-build"} for target in targets)
    needs_node = needs_web or ("process" in targets and args.process_group in (None, "execution"))
    if needs_node:
        node_version = subprocess.check_output(["node", "--version"], text=True).strip()
        if node_version.split(".")[0] != "v24":
            parser.error("CI 验证请使用 Node.js 24")
    if "python" in targets or "process" in targets:
        run([sys.executable, "-m", "pip", "install", "--require-hashes", "-r", "requirements.lock"])
    if "process" in targets:
        from scripts import runtime_tools
        # rg / fd / Node for this process and the tests it starts; not the user's PATH.
        runtime_tools.prepare(ROOT)
    if needs_web:
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        run([npm, "ci"], cwd=ROOT / "web")
    for target in targets:
        if target == "python":
            run([sys.executable, "-m", "ruff", "check", "--select", "F",
                 "redpanda", "thinllm", "scripts", "tests", "console_chat.py", "web_chat.py"])
            run([sys.executable, "-m", "pytest", "--durations=20",
                 "--junitxml=.tools/test-results/python.xml"])
        elif target == "process":
            command = [sys.executable, "-m", "pytest", "-m", "process", "-x", "--durations=20",
                       f"--junitxml=.tools/test-results/process-{args.process_group or 'all'}.xml"]
            if args.process_group:
                command.extend(["--process-group", args.process_group])
            if args.workers > 1:
                command.extend(["-n", str(args.workers), "--dist", "worksteal"])
            run(command)
        else:
            if target in {"web", "web-test"}:
                run([npm, "test"], cwd=ROOT / "web")
            if target in {"web", "web-build"}:
                run([npm, "run", "build"], cwd=ROOT / "web")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    # Direct script execution must also import the process tool preparation.
    sys.path.insert(0, str(ROOT))
    main()
