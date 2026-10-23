"""Export model connection settings from an existing or new Codex login."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from thinllm.auth import codex_auth_path, load_codex_auth
from thinllm.errors import MissingProviderSetting


def _codex_auth(*, login: bool) -> int:
    path = codex_auth_path()
    if not login:
        try:
            load_codex_auth(path)
        except MissingProviderSetting:
            login = True
    if login:
        if not sys.stdin.isatty():
            print("需要 ChatGPT 登录，请在交互终端运行 python -m thinllm auth codex --login。", file=sys.stderr)
            return 1
        codex = shutil.which("codex")
        if codex is None:
            print("未找到 Codex CLI。请先安装并确认 codex 命令可用，再运行本命令："
                  "https://learn.chatgpt.com/docs/codex/cli", file=sys.stderr)
            return 1
        print("即将通过 Codex 完成 ChatGPT 登录；成功后会输出可粘贴的连接配置。", file=sys.stderr)
        result = subprocess.run(
            [codex, "-c", 'cli_auth_credentials_store="file"',
             "-c", 'forced_login_method="chatgpt"', "login"],
            stdout=sys.stderr, check=False,
        )
        if result.returncode:
            print(f"Codex 登录未完成（退出码 {result.returncode}），未输出连接配置。", file=sys.stderr)
            return 1
        try:
            load_codex_auth(path)
        except MissingProviderSetting as error:
            print(f"登录后仍未找到可用的 ChatGPT 认证文件：{path}。{error}", file=sys.stderr)
            return 1
    print(json.dumps({"openai-codex": {"auth_file": path.resolve().as_posix()}}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m thinllm", description="导出可粘贴的模型连接配置")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="复用或获取模型登录")
    providers = auth.add_subparsers(dest="provider", required=True)
    codex = providers.add_parser("codex", help="复用 Codex ChatGPT 登录并输出连接配置")
    codex.add_argument("--login", action="store_true", help="重新通过 Codex 登录，即使认证文件已存在")
    args = parser.parse_args(argv)
    try:
        return _codex_auth(login=args.login)
    except KeyboardInterrupt:
        print("\nCodex 登录已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
