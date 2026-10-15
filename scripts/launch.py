"""Launch the prepared application; --dev enables source hot reload."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    development = "--dev" in sys.argv[1:]
    required = ROOT / ("web/node_modules/vite/bin/vite.js" if development else "web/dist/index.html")
    if not required.is_file() and not {"--help", "-h"}.intersection(sys.argv[1:]):
        raise SystemExit("网页尚未准备好，请先运行 scripts/setup.ps1 或 scripts/setup.sh。")
    from web_chat import main as run_web
    run_web()


if __name__ == "__main__":
    main()
