import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.process
def test_dev_entrypoint_forwards_arguments_from_another_directory(tmp_path):
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "application"
    shutil.copytree(source / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(source / "web_chat.py", root / "web_chat.py")
    # Argument forwarding needs a prepared entrypoint, but never starts Vite.
    vite = root / "web/node_modules/vite/bin/vite.js"
    vite.parent.mkdir(parents=True)
    vite.write_text("throw new Error('Argument help must not start Vite');", encoding="utf-8")
    python = source / "redpanda-env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if os.name == "nt":
        shell = shutil.which("powershell")
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts/dev.ps1"), "--help"]
    else:
        shell = shutil.which("sh")
        command = [shell, str(root / "scripts/dev.sh"), "--help"]
    if not shell or not python.is_file():
        pytest.skip("Project Python or platform shell is not installed")
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "redpanda-env"), str(python.parent.parent)],
                       check=True, capture_output=True)
    else:
        (root / "redpanda-env").symlink_to(python.parent.parent, target_is_directory=True)
    environment = {**os.environ, "PYTHONPATH": str(source), "REDPANDA_HOME": str(tmp_path / "home"), "PYTHONUTF8": "1"}
    environment.pop("REDPANDA_CONFIG", None)
    result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--workspace" in result.stdout
    assert "--dev" in result.stdout
