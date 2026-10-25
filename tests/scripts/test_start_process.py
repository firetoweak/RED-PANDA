"""The ordinary launcher serves the built UI without a Node.js runtime."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import pytest


@pytest.mark.process
def test_start_serves_built_web_without_node_from_another_directory(tmp_path, prepared_application):
    source = Path(__file__).resolve().parents[2]
    root = prepared_application
    # Use the actual application code with a small static page; no frontend build tools.
    (root / "web/dist/assets").mkdir(parents=True)
    (root / "web/dist/index.html").write_text("<!doctype html><title>Prepared konden</title>", encoding="utf-8")
    if os.name == "nt":
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = [str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts/start.ps1")]
        system_path = str(Path(os.environ["SystemRoot"]) / "System32")
    else:
        command = ["/bin/sh", str(root / "scripts/start.sh")]
        helpers = tmp_path / "helpers"
        helpers.mkdir()
        (helpers / "dirname").symlink_to(shutil.which("dirname"))
        system_path = str(helpers)
    environment = {**os.environ, "PATH": system_path, "PYTHONPATH": str(source),
                   "KONDEN_HOME": str(tmp_path / "home"), "PYTHONUTF8": "1"}
    environment.pop("KONDEN_CONFIG", None)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command + ["--port", str(port)], cwd=tmp_path, env=environment,
                                   stdout=log, stderr=log)
        try:
            # Probe this loopback server directly, regardless of machine proxies.
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert process.poll() is None, (tmp_path / "server.log").read_text(encoding="utf-8")
                try:
                    with opener.open(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        assert b"Prepared konden" in response.read()
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
