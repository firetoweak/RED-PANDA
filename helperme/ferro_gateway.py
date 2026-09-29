"""Project-local Ferro Gateway commands; inference still uses HTTP only."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from helperme.llm.config import FERRO_BASE_URL, FERRO_PROJECT_ROOT, load_gateway_config
from helperme.llm.ferro_client import FerroClient


def _ferro_binary() -> Path:
    name = "ferrogw.exe" if os.name == "nt" else "ferrogw"
    return FERRO_PROJECT_ROOT / ".tools" / "ferro" / name


def _server_environment() -> dict[str, str]:
    config = load_gateway_config()
    environment = os.environ.copy()
    environment["MASTER_KEY"] = config.api_key
    environment["GATEWAY_CONFIG"] = str(
        FERRO_PROJECT_ROOT / "ferro" / "config.yaml"
    )
    environment["PORT"] = str(urlsplit(FERRO_BASE_URL).port)
    return environment


def _serve() -> int:
    binary = _ferro_binary()
    if not binary.is_file():
        raise FileNotFoundError(
            f"未安装 Ferro：请先运行 scripts/setup.ps1（Windows）"
            f"或 scripts/setup.sh（macOS/Linux）；预期位置：{binary}"
        )
    environment = _server_environment()
    print(
        f"Gateway login: http://localhost:{environment['PORT']}/login\n"
        f"Gateway key: {environment['MASTER_KEY']}",
        flush=True,
    )
    result = subprocess.run(
        [str(binary), "serve"],
        cwd=FERRO_PROJECT_ROOT,
        env=environment,
        check=False,
    )
    return result.returncode


async def _list_models() -> None:
    async with FerroClient(load_gateway_config()) as client:
        models = await client.list_models()
    for model in models:
        print(model)


def main() -> None:
    parser = argparse.ArgumentParser(description="管理项目内的 Ferro Gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="以前台进程启动 Ferro")
    subparsers.add_parser("models", help="列出 Ferro 当前提供的模型")
    args = parser.parse_args()
    if args.command == "serve":
        raise SystemExit(_serve())
    asyncio.run(_list_models())


if __name__ == "__main__":
    main()
