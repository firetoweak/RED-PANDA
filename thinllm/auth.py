"""Read the ChatGPT credentials managed by Codex."""
from __future__ import annotations

import json
import os
from pathlib import Path

from thinllm.errors import MissingProviderSetting


def codex_auth_path(setting: str = "") -> Path:
    if setting.strip():
        return Path(setting.strip()).expanduser()
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"


def load_codex_auth(path: Path) -> tuple[str, str]:
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise MissingProviderSetting("auth_file（请运行 python -m thinllm auth codex）") from error
    if type(auth) is not dict:
        raise ValueError(f"Codex 认证文件必须是对象：{path}")
    if auth.get("auth_mode") not in (None, "chatgpt") or auth.get("tokens") is None:
        raise MissingProviderSetting("ChatGPT 登录（请运行 python -m thinllm auth codex）")
    tokens = auth["tokens"]
    if type(tokens) is not dict or any(
        type(tokens.get(key)) is not str or not tokens[key].strip()
        for key in ("access_token", "account_id")
    ):
        raise ValueError(f"Codex 认证文件缺少有效的 access_token/account_id：{path}")
    return tokens["access_token"], tokens["account_id"]
