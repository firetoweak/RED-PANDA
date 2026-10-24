from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from konden.config import CONFIG_PATH_ENV
from konden.paths import HOME_PATH_ENV


async def async_main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="工作区路径；缺省使用启动目录",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home() / ".konden-tui",
        help="TUI 自己的数据目录，与 Web 的数据互不相干",
    )
    args = parser.parse_args(argv)
    # 工作区命令沿用启动环境，不看到 TUI 改写的数据目录。
    command_environment = dict(os.environ)
    os.environ[HOME_PATH_ENV] = str(args.home)
    os.environ.pop(CONFIG_PATH_ENV, None)

    from konden.channels.tui.console import run_runtime_console
    from konden.initialize import configure, initialize, needs_configuration

    home = initialize()
    if needs_configuration(home):
        configure(home)
    await run_runtime_console(
        workspace_path=args.workspace, command_environment=command_environment
    )


def main(argv: list[str] | None = None) -> None:
    try:
        asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
