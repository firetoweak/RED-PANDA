import argparse
import os
import subprocess
from pathlib import Path

import uvicorn

from redpanda.channels.web import create_web_app
from redpanda.assistant.host.process_env import host_process_environment


STATIC_DIR = Path(__file__).resolve().parent / "web" / "dist"


def production_app(workspace_path: Path | None = None):
    return create_web_app(workspace_path=workspace_path,
                          static_dir=STATIC_DIR if STATIC_DIR.is_dir() else None)


app = production_app()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dev",
        action="store_true",
        help="同时启动 Vite 开发服务器",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web 监听地址；局域网共享时使用 0.0.0.0",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Web 服务端口",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="显式登记工作区路径；缺省不从启动目录隐式创建",
    )
    args = parser.parse_args(argv)

    frontend = None
    if args.dev:
        web = Path(__file__).parent / "web"
        frontend = subprocess.Popen(
            ["node", web / "node_modules" / "vite" / "bin" / "vite.js"],
            cwd=web,
            env=host_process_environment(),
            # Uvicorn uses Ctrl+C for reloads on Windows; Vite must survive them.
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    try:
        uvicorn.run(
            "web_chat:app" if args.dev else production_app(args.workspace),
            host=args.host,
            port=args.port,
            reload=args.dev,
            reload_dirs=[str(Path(__file__).parent)] if args.dev else None,
            timeout_graceful_shutdown=1,
        )
    finally:
        if frontend is not None and frontend.poll() is None:
            frontend.terminate()
            frontend.wait()


if __name__ == "__main__":
    main()
