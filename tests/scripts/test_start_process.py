"""The ordinary launcher serves the built UI without a Node.js runtime."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest


@pytest.mark.process
def test_start_serves_built_web_without_node_from_another_directory(tmp_path):
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "application"
    root.mkdir()
    shutil.copytree(source / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(source / "web_chat.py", root / "web_chat.py")
    # Use the actual application code with a small static page; no frontend build tools.
    (root / "web/dist/assets").mkdir(parents=True)
    (root / "web/dist/index.html").write_text("<!doctype html><title>Prepared RED PANDA</title>", encoding="utf-8")
    python = source / "redpanda-env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        pytest.skip("Project Python is not installed")
    if os.name == "nt":
        # A junction does not require Developer Mode or elevated symlink privileges.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "redpanda-env"), str(python.parent.parent)],
                       check=True, capture_output=True)
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = [str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts/start.ps1")]
        system_path = str(Path(os.environ["SystemRoot"]) / "System32")
    else:
        (root / "redpanda-env").symlink_to(python.parent.parent, target_is_directory=True)
        command = ["/bin/sh", str(root / "scripts/start.sh")]
        helpers = tmp_path / "helpers"
        helpers.mkdir()
        (helpers / "dirname").symlink_to(shutil.which("dirname"))
        system_path = str(helpers)
    environment = {**os.environ, "PATH": system_path, "PYTHONPATH": str(source),
                   "REDPANDA_HOME": str(tmp_path / "home"), "PYTHONUTF8": "1"}
    environment.pop("REDPANDA_CONFIG", None)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command + ["--port", str(port)], cwd=tmp_path, env=environment,
                                   stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert process.poll() is None, (tmp_path / "server.log").read_text(encoding="utf-8")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        assert b"Prepared RED PANDA" in response.read()
                    break
                except URLError:
                    time.sleep(0.1)
            else:
                pytest.fail("Prepared Web did not become available")
        finally:
            if os.name == "nt":
                subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                                "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                process.terminate()
            process.wait(timeout=10)
