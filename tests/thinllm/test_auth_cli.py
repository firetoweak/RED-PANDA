import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
from oauth_cli_kit.models import OAuthToken
from oauth_cli_kit import flow, server
import pytest

from thinllm import __main__ as cli
from thinllm import auth


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    monkeypatch.setattr(auth.Path, "home", lambda: tmp_path)
    root = tmp_path / "Codex 登录 示例"
    root.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(root))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    return root


def write_login(root, token="private-access"):
    (root / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "tokens": {"access_token": token, "account_id": "private-account"},
    }), encoding="utf-8")


def test_existing_login_exports_only_a_reference_without_oauth_or_file_writes(codex_home, monkeypatch, capsys):
    write_login(codex_home)
    path = codex_home / "auth.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "login_oauth_interactive", lambda **k: pytest.fail("must reuse login"))
    assert cli.main(["auth", "codex"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"openai-codex": {"auth_file": path.resolve().as_posix()}}
    assert "private-" not in output.out + output.err
    assert output.err == ""
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not auth.thinllm_auth_path().exists()


@pytest.mark.parametrize("existing", [None, {"auth_mode": "apikey", "OPENAI_API_KEY": "private-key"}])
def test_first_login_saves_separate_credentials_and_keeps_stdout_copyable(codex_home, monkeypatch, capsys, existing):
    shared = codex_home / "auth.json"
    if existing is not None:
        shared.write_text(json.dumps(existing), encoding="utf-8")
    before = shared.read_bytes() if shared.exists() else None

    def login(*, print_fn, prompt_fn, storage, originator):
        assert originator == "thinllm"
        print_fn("[cyan]浏览器授权完成[/cyan]")
        storage.save(OAuthToken("private-access", "private-refresh", 10**15, "private-account"))

    monkeypatch.setattr(cli, "login_oauth_interactive", login)
    assert cli.main(["auth", "codex"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["openai-codex"]["auth_file"] == auth.thinllm_auth_path().resolve().as_posix()
    assert "浏览器授权完成" in output.err and "[cyan]" not in output.err
    assert "private-" not in output.out + output.err
    assert auth.load_codex_auth(auth.thinllm_auth_path()) == ("private-access", "private-account")
    assert (shared.read_bytes() if shared.exists() else None) == before


def test_explicit_login_creates_owned_session_without_replacing_codex(codex_home, monkeypatch, capsys):
    write_login(codex_home)
    before = (codex_home / "auth.json").read_bytes()
    monkeypatch.setattr(cli, "login_oauth_interactive", lambda **kw: kw["storage"].save(
        OAuthToken("new-access", "new-refresh", 10**15, "new-account")))
    assert cli.main(["auth", "codex", "--login"]) == 0
    assert json.loads(capsys.readouterr().out)["openai-codex"]["auth_file"] == auth.thinllm_auth_path().as_posix()
    assert (codex_home / "auth.json").read_bytes() == before
    monkeypatch.setattr(cli, "login_oauth_interactive", lambda **kw: pytest.fail("must reuse owned login"))
    assert cli.main(["auth", "codex"]) == 0


def test_noninteractive_missing_login_does_not_wait_or_export(codex_home, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "login_oauth_interactive", lambda **k: pytest.fail("must not wait"))
    assert cli.main(["auth", "codex"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "交互终端" in output.err
    assert not auth.thinllm_auth_path().exists()


def test_corrupt_persisted_auth_is_exposed_without_starting_login(codex_home, monkeypatch):
    path = codex_home / "auth.json"
    path.write_text("{", encoding="utf-8")
    monkeypatch.setattr(cli, "login_oauth_interactive", lambda **k: pytest.fail("must not hide corruption"))
    with pytest.raises(json.JSONDecodeError):
        cli.main(["auth", "codex"])
    assert path.read_text(encoding="utf-8") == "{"


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("OAuth refused")])
def test_failed_login_keeps_existing_credentials_without_export(codex_home, monkeypatch, capsys, error):
    storage = auth.CodexTokenStorage(auth.thinllm_auth_path())
    storage.save(OAuthToken("old", "refresh", 10**15, "account"))
    before = storage.path.read_bytes()

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(cli, "login_oauth_interactive", fail)
    if isinstance(error, KeyboardInterrupt):
        assert cli.main(["auth", "codex", "--login"]) == 130
    else:
        with pytest.raises(RuntimeError, match="OAuth refused"):
            cli.main(["auth", "codex", "--login"])
    assert capsys.readouterr().out == ""
    assert storage.path.read_bytes() == before


@pytest.mark.parametrize("value", ["code-only", "http://localhost:1455/auth/callback?code=x",
                                  "https://evil.invalid/?code=x&state=y"])
def test_manual_callback_requires_full_url_and_state(monkeypatch, value):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(value + "\n"))
    with pytest.raises(ValueError, match="完整授权回调"):
        cli._prompt_callback("paste")


def test_oauth_pkce_uses_real_loopback_callback_and_storage_without_codex(codex_home, monkeypatch, capsys):
    urls = []
    listeners = []

    def start(state, on_code):
        listener = server._OAuthServer(("127.0.0.1", 0), state, on_code)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        listeners.append(listener)
        return listener, None

    def open_browser(url):
        urls.append(url)
        params = parse_qs(urlsplit(url).query)
        callback = f"http://127.0.0.1:{listeners[0].server_port}/auth/callback"
        with httpx.Client(trust_env=False) as client:
            assert client.get(callback, params={"code": "invalid", "state": "wrong"}).status_code == 400
            assert client.get(callback, params={"code": "test-code", "state": params["state"][0]}).status_code == 200
        return True

    def exchange(request):
        params = parse_qs(request.content.decode())
        authorize = parse_qs(urlsplit(urls[0]).query)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(params["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        assert request.url == "https://auth.openai.com/oauth/token"
        assert params["code"] == ["test-code"] and params["grant_type"] == ["authorization_code"]
        assert params["redirect_uri"] == authorize["redirect_uri"]
        assert authorize["code_challenge"] == [challenge] and authorize["code_challenge_method"] == ["S256"]
        claims = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "account"}}).encode()).rstrip(b"=").decode()
        return httpx.Response(200, json={"access_token": f"e30.{claims}.sig", "refresh_token": "refresh", "expires_in": 3600})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(flow, "_start_local_server", start)
    monkeypatch.setattr(flow.webbrowser, "open", open_browser)
    monkeypatch.setattr(flow, "_should_open_browser", lambda: True)
    monkeypatch.setattr(flow.httpx, "AsyncClient", lambda **kw: async_client(transport=httpx.MockTransport(exchange)))
    assert cli.main(["auth", "codex"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["openai-codex"]["auth_file"] == auth.thinllm_auth_path().as_posix()
    assert auth.CodexTokenStorage(auth.thinllm_auth_path()).load().account_id == "account"
    assert "refresh_token" not in output.out + output.err


@pytest.mark.process
def test_python_module_command_exports_valid_configuration(codex_home):
    write_login(codex_home)
    result = subprocess.run(
        [sys.executable, "-m", "thinllm", "auth", "codex"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "CODEX_HOME": str(codex_home), "USERPROFILE": str(codex_home.parent),
             "HOME": str(codex_home.parent)},
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert json.loads(result.stdout) == {"openai-codex": {
        "auth_file": (codex_home / "auth.json").resolve().as_posix(),
    }}
    assert result.stderr == "" and "private-" not in result.stdout
