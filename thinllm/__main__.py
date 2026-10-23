"""Export model connection settings from an existing or new Codex login."""
from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import parse_qs, urlsplit

from oauth_cli_kit import login_oauth_interactive

from thinllm.auth import CodexTokenStorage, codex_auth_path, load_codex_auth, thinllm_auth_path
from thinllm.errors import LLMProviderError, LLMTransientError, MissingProviderSetting


def _login_message(message: str) -> None:
    print(re.sub(r"\[/?(?:cyan|yellow|dim)\]", "", message), file=sys.stderr)


def _prompt_callback(message: str) -> str:
    print(message, file=sys.stderr)
    value = sys.stdin.readline().strip()
    url = urlsplit(value)
    params = parse_qs(url.query)
    if (url.scheme != "http" or url.netloc != "localhost:1455" or url.path != "/auth/callback"
            or not params.get("code") or not params.get("state")):
        raise ValueError("请粘贴包含 code 和 state 的完整授权回调 URL。")
    return value


def _codex_auth(*, login: bool) -> int:
    path = codex_auth_path()
    if not login:
        try:
            load_codex_auth(path, refresh=True)
        except MissingProviderSetting:
            login = True
    if login:
        if not sys.stdin.isatty():
            print("需要 ChatGPT 登录，请在交互终端运行 python -m thinllm auth codex --login。", file=sys.stderr)
            return 1
        path = thinllm_auth_path()
        print("即将打开浏览器完成 ChatGPT 授权；成功后会输出可粘贴的连接配置。", file=sys.stderr)
        login_oauth_interactive(print_fn=_login_message, prompt_fn=_prompt_callback,
                                originator="thinllm", storage=CodexTokenStorage(path))
        load_codex_auth(path)
    print(json.dumps({"openai-codex": {"auth_file": path.resolve().as_posix()}}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m thinllm", description="导出可粘贴的模型连接配置")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="复用或获取模型登录")
    providers = auth.add_subparsers(dest="provider", required=True)
    codex = providers.add_parser("codex", help="复用 Codex ChatGPT 登录并输出连接配置")
    codex.add_argument("--login", action="store_true", help="重新进行 OAuth 授权，凭据独立保存在 thinllm 目录")
    args = parser.parse_args(argv)
    try:
        return _codex_auth(login=args.login)
    except KeyboardInterrupt:
        print("\nCodex 登录已取消。", file=sys.stderr)
        return 130
    except (LLMTransientError, LLMProviderError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
