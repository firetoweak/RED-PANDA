"""Own thinllm OAuth credentials; read shared Codex credentials without writing."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time

from filelock import FileLock
import httpx
from oauth_cli_kit.models import OAuthToken
from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER

from thinllm.errors import LLMAuthenticationError, LLMProviderError, LLMTransientError, MissingProviderSetting


def thinllm_auth_path() -> Path:
    return Path.home() / ".thinllm" / "codex.json"


def codex_auth_path(setting: str = "") -> Path:
    if setting.strip():
        return Path(setting.strip()).expanduser()
    managed = thinllm_auth_path()
    if managed.exists():
        return managed
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"


def _read_auth(path: Path) -> dict:
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise MissingProviderSetting("auth_file（请运行 python -m thinllm auth codex）") from error
    if type(auth) is not dict:
        raise ValueError(f"Codex 认证文件必须是对象：{path}")
    return auth


def load_codex_auth(path: Path, *, refresh: bool = False) -> tuple[str, str]:
    auth = _read_auth(path)
    if auth.get("auth_mode") == "thinllm":
        token = _managed_token(auth, path)
        if refresh and token.expires <= time.time() * 1000 + 60_000:
            storage = CodexTokenStorage(path)
            with FileLock(str(path) + ".lock", timeout=30):
                # Other requests or processes may have renewed the same session.
                token = storage.load()
                if token.expires <= time.time() * 1000 + 60_000:
                    token = _refresh_token(token)
                    storage._save(token)
        return token.access, token.account_id
    if auth.get("auth_mode") not in (None, "chatgpt", "apikey"):
        raise ValueError(f"Codex 认证文件的 auth_mode 无效：{path}")
    if auth.get("auth_mode") == "apikey" or auth.get("tokens") is None:
        raise MissingProviderSetting("ChatGPT 登录（请运行 python -m thinllm auth codex）")
    tokens = auth["tokens"]
    if type(tokens) is not dict or any(
        type(tokens.get(key)) is not str or not tokens[key].strip()
        for key in ("access_token", "account_id")
    ):
        raise ValueError(f"Codex 认证文件缺少有效的 access_token/account_id：{path}")
    return tokens["access_token"], tokens["account_id"]


def _managed_token(auth: dict, path: Path) -> OAuthToken:
    fields = {"access_token", "refresh_token", "account_id", "expires_at"}
    tokens = auth.get("tokens")
    if (set(auth) != {"auth_mode", "tokens"} or auth["auth_mode"] != "thinllm"
            or type(tokens) is not dict or set(tokens) != fields
            or any(type(tokens[key]) is not str or not tokens[key].strip()
                   for key in fields - {"expires_at"})
            or type(tokens["expires_at"]) is not int or tokens["expires_at"] <= 0):
        raise ValueError(f"thinllm 认证文件格式错误：{path}")
    return OAuthToken(access=tokens["access_token"], refresh=tokens["refresh_token"],
                      expires=tokens["expires_at"], account_id=tokens["account_id"])


@dataclass(frozen=True)
class CodexTokenStorage:
    """OAuth library storage port, exclusively for thinllm-owned files."""

    path: Path

    def get_token_path(self) -> Path:
        return self.path

    def load(self) -> OAuthToken:
        return _managed_token(_read_auth(self.path), self.path)

    def save(self, token: OAuthToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(str(self.path) + ".lock", timeout=30):
            self._save(token)

    def _save(self, token: OAuthToken) -> None:
        auth = {"auth_mode": "thinllm", "tokens": {
            "access_token": token.access, "refresh_token": token.refresh,
            "account_id": token.account_id, "expires_at": token.expires,
        }}
        _managed_token(auth, self.path)
        # Same-directory replacement keeps readers on a complete token set.
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".codex-")
        pending = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                json.dump(auth, temporary)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(pending, self.path)
        finally:
            pending.unlink(missing_ok=True)


def _refresh_token(token: OAuthToken) -> OAuthToken:
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(OPENAI_CODEX_PROVIDER.token_url, data={
                "grant_type": "refresh_token", "client_id": OPENAI_CODEX_PROVIDER.client_id,
                "refresh_token": token.refresh,
            })
    except httpx.TransportError as error:
        raise LLMTransientError("Codex 授权续期连接失败，请重试。") from error
    needs_login = response.status_code in (401, 403)
    if response.status_code == 400:
        failure = response.json()
        if type(failure) is not dict:
            raise ValueError("Codex 授权续期返回了无效的错误信息。")
        error = failure.get("error")
        code = error.get("code") if type(error) is dict else error
        needs_login = type(code) is str and code in {
            "invalid_grant", "refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated",
        }
    if needs_login:
        raise LLMAuthenticationError(
            "Codex 授权无法续期，请运行 python -m thinllm auth codex --login 后更新连接配置。")
    if response.status_code in (408, 425, 429, 500, 502, 503, 504):
        raise LLMTransientError(f"Codex 授权续期暂时失败（HTTP {response.status_code}），请重试。")
    if response.status_code != 200:
        raise LLMProviderError(f"Codex 授权续期失败（HTTP {response.status_code}）。")
    payload = response.json()
    if (type(payload) is not dict
            or any(type(payload.get(key)) is not str or not payload[key].strip()
                   for key in ("access_token", "refresh_token"))
            or type(payload.get("expires_in")) is not int or payload["expires_in"] <= 0):
        raise ValueError("Codex 授权续期返回了无效的凭据。")
    return OAuthToken(access=payload["access_token"], refresh=payload["refresh_token"],
                      expires=int(time.time() * 1000) + payload["expires_in"] * 1000,
                      account_id=token.account_id)


class CodexAuth(httpx.Auth):
    def __init__(self, path: Path):
        self.path = path

    async def async_auth_flow(self, request):
        access, account = await asyncio.to_thread(load_codex_auth, self.path, refresh=True)
        request.headers["Authorization"] = f"Bearer {access}"
        request.headers["ChatGPT-Account-Id"] = account
        yield request
