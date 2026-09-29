from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


async def async_main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from helperme.channels.tui.console import run_runtime_console

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="工作区路径；缺省使用启动目录",
    )
    args = parser.parse_args(argv)
    await run_runtime_console(workspace_path=args.workspace)


def main(argv: list[str] | None = None) -> None:
    try:
        asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
