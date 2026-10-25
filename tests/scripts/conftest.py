from pathlib import Path
import shutil
import site
import sys
import venv

import pytest


@pytest.fixture
def prepared_application(tmp_path):
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "application"
    shutil.copytree(source / "scripts", root / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    shutil.copyfile(source / "web_chat.py", root / "web_chat.py")
    environment = root / "konden-env"
    venv.create(environment, with_pip=False)
    # Reuse the locked dependencies, including their normal .pth initialization.
    packages = environment / ("Lib/site-packages" if sys.platform == "win32"
                              else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    (packages / "test-dependencies.pth").write_text(
        f"import site; [site.addsitedir(path) for path in {site.getsitepackages()!r}]\n", encoding="utf-8",
    )
    return root
