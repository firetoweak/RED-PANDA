import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.process
def test_dev_entrypoint_forwards_arguments_from_another_directory(tmp_path):
    root = Path(__file__).resolve().parents[2]
    if not (root / "web/node_modules/vite/bin/vite.js").is_file():
        pytest.skip("Web dependencies are not installed")
    if os.name == "nt":
        shell = shutil.which("powershell")
        python = root / "redpanda-env/Scripts/python.exe"
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts/dev.ps1"), "--help"]
    else:
        shell = shutil.which("sh")
        python = root / "redpanda-env/bin/python"
        command = [shell, str(root / "scripts/dev.sh"), "--help"]
    if not shell or not python.is_file():
        pytest.skip("Project Python or platform shell is not installed")
    environment = {**os.environ, "REDPANDA_HOME": str(tmp_path / "home"), "PYTHONUTF8": "1"}
    environment.pop("REDPANDA_CONFIG", None)
    result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--workspace" in result.stdout
    assert "--dev" in result.stdout
