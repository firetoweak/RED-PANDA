import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs

import httpx
from oauth_cli_kit.models import OAuthToken
import pytest

from thinllm import auth, create_client, resolve_endpoint
from thinllm.errors import LLMAuthenticationError, LLMProviderError, LLMTransientError


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(auth.Path, "home", lambda: tmp_path)
    storage = auth.CodexTokenStorage(auth.thinllm_auth_path())
    storage.save(OAuthToken("old-access", "old-refresh", 1, "account"))
    return storage


def mock_issuer(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(auth.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handler)))


def renewed():
    return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})


def test_probe_is_read_only_and_client_renews_before_sending(storage, monkeypatch):
    before = storage.path.read_bytes()
    endpoint = resolve_endpoint("openai-codex/test", {"auth_file": str(storage.path)})
    assert endpoint.api_key == "old-access" and storage.path.read_bytes() == before
    refreshes = []

    def issue(request):
        assert request.url == "https://auth.openai.com/oauth/token"
        params = parse_qs(request.content.decode())
        assert params == {"grant_type": ["refresh_token"], "refresh_token": ["old-refresh"],
                          "client_id": [auth.OPENAI_CODEX_PROVIDER.client_id]}
        refreshes.append(request)
        return renewed()

    requests = []

    def complete(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer new-access"
        assert request.headers["chatgpt-account-id"] == "account"
        event = {"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "OK"},
            ]}], "usage": {"input_tokens": 1, "output_tokens": 1},
        }}
        return httpx.Response(200, content="data: " + json.dumps(event) + "\n\n")

    async def run():
        async with create_client(endpoint, transport=httpx.MockTransport(complete)) as client:
            await client.chat([], "openai-codex/test")
            await client.chat([], "openai-codex/test")

    mock_issuer(monkeypatch, issue)
    asyncio.run(run())
    assert len(refreshes) == 1 and len(requests) == 2
    token = storage.load()
    assert token.access == "new-access" and token.refresh == "new-refresh"
    assert token.expires > 1
    if os.name != "nt":
        assert storage.path.stat().st_mode & 0o777 == 0o600


def test_concurrent_requests_recheck_after_another_refresh(storage, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def issue(request):
        calls.append(request)
        started.set()
        assert release.wait(5)
        return renewed()

    mock_issuer(monkeypatch, issue)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(auth.load_codex_auth, storage.path, refresh=True)
        assert started.wait(5)
        second = pool.submit(auth.load_codex_auth, storage.path, refresh=True)
        release.set()
        assert first.result() == second.result() == ("new-access", "account")
    assert len(calls) == 1
    assert storage.load().refresh == "new-refresh"


@pytest.mark.parametrize(("status", "error"), [(400, LLMAuthenticationError), (401, LLMAuthenticationError),
                                              (503, LLMTransientError), (418, LLMProviderError)])
def test_failed_refresh_preserves_credentials_and_does_not_expose_tokens(storage, monkeypatch, status, error):
    before = storage.path.read_bytes()
    mock_issuer(monkeypatch, lambda _: httpx.Response(status, json={
        "error": "invalid_grant", "error_description": "private-token-from-provider",
    }))
    with pytest.raises(error) as caught:
        auth.load_codex_auth(storage.path, refresh=True)
    assert "private-token" not in str(caught.value)
    if error is LLMAuthenticationError:
        assert "--login" in str(caught.value)
    assert storage.path.read_bytes() == before


@pytest.mark.parametrize(("failure", "error"), [
    ({"error": {"code": "refresh_token_reused"}}, LLMAuthenticationError),
    ({"error": "invalid_client"}, LLMProviderError),
])
def test_refresh_rejection_distinguishes_revoked_session_from_client_error(storage, monkeypatch, failure, error):
    mock_issuer(monkeypatch, lambda _: httpx.Response(400, json=failure))
    with pytest.raises(error) as caught:
        auth.load_codex_auth(storage.path, refresh=True)
    assert type(caught.value) is error


@pytest.mark.parametrize("payload", [[], {"access_token": "new"},
                                     {"access_token": "new", "refresh_token": "r", "expires_in": True}])
def test_bad_refresh_response_is_exposed_without_overwriting(storage, monkeypatch, payload):
    before = storage.path.read_bytes()
    mock_issuer(monkeypatch, lambda _: httpx.Response(200, json=payload))
    with pytest.raises(ValueError):
        auth.load_codex_auth(storage.path, refresh=True)
    assert storage.path.read_bytes() == before


def test_connection_failure_is_transient_and_unknown_errors_propagate(storage, monkeypatch):
    before = storage.path.read_bytes()

    def fail(request):
        raise httpx.ConnectError("connection failed", request=request)

    mock_issuer(monkeypatch, fail)
    with pytest.raises(LLMTransientError):
        auth.load_codex_auth(storage.path, refresh=True)

    def unknown(**kwargs):
        raise LookupError("unknown")

    monkeypatch.setattr(auth.httpx, "Client", unknown)
    with pytest.raises(LookupError, match="unknown"):
        auth.load_codex_auth(storage.path, refresh=True)
    assert storage.path.read_bytes() == before


def test_shared_codex_refresh_token_is_never_used_or_rewritten(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
        "access_token": "shared-access", "refresh_token": "shared-refresh", "account_id": "shared-account",
    }}), encoding="utf-8")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.setattr(auth.httpx, "Client", lambda **kw: pytest.fail("shared credentials are read only"))
    assert auth.load_codex_auth(path, refresh=True) == ("shared-access", "shared-account")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert list(tmp_path.iterdir()) == [path]


def test_failed_atomic_replace_keeps_previous_complete_token_set(storage, monkeypatch):
    before = storage.path.read_bytes()

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(auth.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        storage.save(OAuthToken("new", "refresh", 10**15, "account"))
    assert storage.path.read_bytes() == before
    assert not list(storage.path.parent.glob(".codex-*"))


def test_corrupt_owned_file_is_exposed_without_falling_back_to_codex(storage, tmp_path, monkeypatch):
    shared = tmp_path / ".codex"
    shared.mkdir()
    (shared / "auth.json").write_text('{"tokens":{"access_token":"shared","account_id":"a"}}', encoding="utf-8")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    data = json.loads(storage.path.read_text(encoding="utf-8"))
    data["tokens"]["expires_at"] = "yesterday"
    storage.path.write_text(json.dumps(data), encoding="utf-8")
    assert auth.codex_auth_path() == storage.path
    with pytest.raises(ValueError, match="thinllm 认证文件格式错误"):
        auth.load_codex_auth(auth.codex_auth_path(), refresh=True)


@pytest.mark.process
def test_two_processes_share_one_rotating_refresh(storage):
    requests = []

    class Issuer(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            # Keep the first exchange open while the other process reaches the file.
            time.sleep(0.3)
            data = json.dumps({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    issuer = ThreadingHTTPServer(("127.0.0.1", 0), Issuer)
    threading.Thread(target=issuer.serve_forever, daemon=True).start()
    script = """
from dataclasses import replace
from pathlib import Path
import json, sys
from thinllm import auth
auth.OPENAI_CODEX_PROVIDER = replace(auth.OPENAI_CODEX_PROVIDER, token_url=sys.argv[2])
print(json.dumps(auth.load_codex_auth(Path(sys.argv[1]), refresh=True)))
"""
    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen(
                [sys.executable, "-c", script, str(storage.path), f"http://127.0.0.1:{issuer.server_port}/token"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, stderr
            assert json.loads(stdout) == ["new-access", "account"]
        assert len(requests) == 1 and requests[0]["refresh_token"] == ["old-refresh"]
        assert storage.load().refresh == "new-refresh"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        issuer.shutdown()
        issuer.server_close()
