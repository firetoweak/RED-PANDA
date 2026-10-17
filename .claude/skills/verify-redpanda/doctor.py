#!/usr/bin/env python3
"""Read-only check that a RED PANDA Web process is the instance this run started."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Web origin, for example http://127.0.0.1:8876")
    parser.add_argument("--home", required=True, type=Path, help="REDPANDA_HOME for this run")
    parser.add_argument("--pid-file", required=True, type=Path, help="pid file written by Launch")
    parser.add_argument("--repo", type=Path, help="repository root; default walks up from this file")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--require-model", action="store_true")
    parser.add_argument("--require-sandbox", action="store_true")
    args = parser.parse_args()

    url = args.url.rstrip("/")
    home = args.home.expanduser().resolve()
    repo = (args.repo or find_repo()).resolve()
    report: dict = {
        "ok": False,
        "url": url,
        "home": str(home),
        "repo": str(repo),
        "blockers": [],
        "warnings": [],
    }

    pid = read_pid(args.pid_file, report)
    if pid is not None:
        report["pid"] = pid
        inspect_process(pid, home, report)

    frontend = http_text(url + "/", args.timeout, report, "frontend")
    report["frontend_title_ok"] = frontend is not None and "<title>RED PANDA</title>" in frontend
    if frontend is not None and not report["frontend_title_ok"]:
        report["blockers"].append("GET / did not return the RED PANDA page; build web/dist and restart this process")

    workspaces = http_json(url + "/api/workspaces", args.timeout, report, "workspaces")
    if isinstance(workspaces, list):
        report["workspace_count"] = len(workspaces)
        report["workspace_names"] = [
            item.get("name") for item in workspaces if isinstance(item, dict)
        ]
    elif workspaces is not None:
        report["blockers"].append("/api/workspaces did not return a list")

    settings = http_json(url + "/api/model-settings", args.timeout, report, "model_settings")
    describe_models(settings, home, report)
    describe_sandbox(pid, repo, report)
    describe_events(url, args.timeout, report)

    if args.require_model and not report.get("live_model_ready"):
        report["blockers"].append("no configured provider for the default model")
    if args.require_sandbox and not report.get("sandbox", {}).get("ready"):
        report["blockers"].append(report.get("sandbox", {}).get("reason", "sandbox is not ready"))

    report["ok"] = not report["blockers"]
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 1


def find_repo() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "web_chat.py").is_file():
            return parent
    raise SystemExit("pass --repo; web_chat.py was not found above doctor.py")


def read_pid(path: Path, report: dict) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
        pid = int(text)
    except (OSError, ValueError) as error:
        report["blockers"].append(f"pid file is not a single integer: {path} ({error})")
        return None
    if pid <= 0:
        report["blockers"].append(f"pid file is not a live pid: {path}")
        return None
    try:
        os.kill(pid, 0)
    except OSError as error:
        report["blockers"].append(f"pid {pid} is not running ({error})")
        return None
    return pid


def inspect_process(pid: int, home: Path, report: dict) -> None:
    proc = Path("/proc") / str(pid)
    if not proc.is_dir():
        report["pid_cmdline_has_web_chat"] = None
        report["home_matches_process"] = None
        report["warnings"].append("process environment was not read; confirm the API home below")
        return
    cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    report["pid_cmdline_has_web_chat"] = "web_chat.py" in cmdline
    if not report["pid_cmdline_has_web_chat"]:
        report["blockers"].append(f"pid {pid} is not web_chat.py")
    actual = process_env(pid, "REDPANDA_HOME")
    report["process_home"] = actual
    report["home_matches_process"] = actual is not None and Path(actual).resolve() == home
    if not report["home_matches_process"]:
        report["blockers"].append("process REDPANDA_HOME does not match --home")


def process_env(pid: int, name: str) -> str | None:
    raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    prefix = f"{name}=".encode()
    for item in raw.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix):].decode("utf-8", "replace")
    return None


def http_text(url: str, timeout: float, report: dict, label: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        report["blockers"].append(f"{label} request failed: {error}")
        return None


def http_json(url: str, timeout: float, report: dict, label: str):
    body = http_text(url, timeout, report, label)
    if body is None:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        report["blockers"].append(f"{label} response is not JSON: {error}")
        return None


def describe_models(settings, home: Path, report: dict) -> None:
    if not isinstance(settings, dict):
        if settings is not None:
            report["blockers"].append("/api/model-settings did not return an object")
        report["live_model_ready"] = False
        return
    leaked = credential_fields(settings)
    if leaked:
        report["blockers"].append("model settings response included credential fields: " + ", ".join(leaked))
    connections = settings.get("connections_path")
    report["connections_path"] = connections if isinstance(connections, str) else None
    expected = home / "connections.json"
    report["home_matches_api"] = (
        isinstance(connections, str) and Path(connections).resolve() == expected.resolve()
    )
    if not report["home_matches_api"]:
        report["blockers"].append("connections_path is not this run's REDPANDA_HOME")
    config = settings.get("config") if isinstance(settings.get("config"), dict) else {}
    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    default = model.get("default")
    report["default_model"] = default if isinstance(default, str) else None
    providers = []
    for item in settings.get("providers") or []:
        if not isinstance(item, dict):
            continue
        providers.append({
            "provider": item.get("provider"),
            "configured": item.get("configured") is True,
            "missing": item.get("missing"),
        })
    report["providers"] = providers
    provider = default.split("/", 1)[0] if isinstance(default, str) and "/" in default else None
    ready = any(item["provider"] == provider and item["configured"] for item in providers)
    report["live_model_ready"] = ready
    if not ready:
        report["warnings"].append(
            "default model provider is not configured; skip chat send, 测试, and tool rounds"
        )


def credential_fields(value) -> list[str]:
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key.lower() in {"api_key", "apikey", "token", "secret", "authorization", "password"}:
                    found.append(str(key))
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def describe_sandbox(pid: int | None, repo: Path, report: dict) -> None:
    system = sys.platform
    configured = process_env(pid, "REDPANDA_FILESYSTEM_EXECUTABLE") if pid and Path(f"/proc/{pid}").is_dir() else None
    if configured:
        executable = Path(configured)
    else:
        name = "redpanda-agentfs.exe" if os.name == "nt" else "redpanda-agentfs"
        executable = repo / "redpanda" / "filesystem" / "bin" / name
    sandbox = {
        "platform": system,
        "executable": str(executable),
        "executable_present": executable.is_file(),
        "ready": False,
        "reason": "",
    }
    if system == "darwin":
        sandbox["reason"] = "macOS file view is not implemented"
    elif not executable.is_file():
        sandbox["reason"] = "native sandbox binary is absent; run scripts/build_agentfs.py"
    elif system.startswith("linux"):
        fuse = os.access("/dev/fuse", os.R_OK | os.W_OK)
        mount = shutil.which("fusermount3")
        sandbox["fuse_device"] = fuse
        sandbox["fusermount3"] = mount is not None
        if fuse and mount:
            sandbox["ready"] = True
            sandbox["reason"] = "linux sandbox binary, /dev/fuse, and fusermount3 are present"
        else:
            sandbox["reason"] = "linux file view needs a writable /dev/fuse and fusermount3"
    elif os.name == "nt":
        sandbox["ready"] = True
        sandbox["reason"] = "sandbox binary is present; WinFsp still has to be installed on the machine"
    else:
        sandbox["reason"] = f"file view is not implemented for {system}"
    report["sandbox"] = sandbox
    if not sandbox["ready"]:
        report["warnings"].append(sandbox["reason"] + "; skip file rewind and coding tool rounds")


def describe_events(url: str, timeout: float, report: dict) -> None:
    request = urllib.request.Request(
        url + "/api/events",
        headers={"Accept": "text/event-stream"},
    )
    event = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while True:
                line = response.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("event:"):
                    event = text.split(":", 1)[1].strip()
                elif text.startswith("data:") and event == "connected":
                    payload = json.loads(text.split(":", 1)[1])
                    connection_id = payload.get("connection_id")
                    if isinstance(connection_id, str) and connection_id.startswith("connection-"):
                        report["sse_connected"] = True
                        report["connection_id"] = connection_id
                        return
                    report["blockers"].append("connected event did not include connection_id")
                    report["sse_connected"] = False
                    return
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        report["blockers"].append(f"event stream failed: {error}")
    report["sse_connected"] = False
    if not any(item.startswith("event stream") for item in report["blockers"]):
        report["blockers"].append("event stream did not emit connected")


if __name__ == "__main__":
    sys.exit(main())
