import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.process
def test_dev_entrypoint_forwards_arguments_from_another_directory(tmp_path, prepared_application):
    source = Path(__file__).resolve().parents[2]
    root = prepared_application
    # Argument forwarding needs a prepared entrypoint, but never starts Vite.
    vite = root / "web/node_modules/vite/bin/vite.js"
    vite.parent.mkdir(parents=True)
    vite.write_text("throw new Error('Argument help must not start Vite');", encoding="utf-8")
    if os.name == "nt":
        shell = shutil.which("powershell")
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts/dev.ps1"), "--help"]
    else:
        shell = shutil.which("sh")
        command = [shell, str(root / "scripts/dev.sh"), "--help"]
    assert shell is not None, "Development launcher requires the platform shell"
    environment = {**os.environ, "PYTHONPATH": str(source), "KONDEN_HOME": str(tmp_path / "home"), "PYTHONUTF8": "1"}
    environment.pop("KONDEN_CONFIG", None)
    result = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True,
                            text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--workspace" in result.stdout
    assert "--dev" in result.stdout
