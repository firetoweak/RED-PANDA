"""Native titles are optional display metadata, never conversation identity or history."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest

from konden.assistant.host.codex_titles import read_codex_titles


IDENTITY = "1" * 32
THREAD_ID = str(UUID(IDENTITY))


def run_reply(response):
    process = SimpleNamespace(
        returncode=0, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
        stdout=SimpleNamespace(readline=AsyncMock(side_effect=[
            b'{"id":0,"result":{}}\n', (json.dumps(response) + "\n").encode(),
        ])), stderr=SimpleNamespace(read=AsyncMock(return_value=b"")), wait=AsyncMock(), kill=Mock(),
    )
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        return asyncio.run(read_codex_titles([IDENTITY]))


def test_missing_local_codex_or_unset_title_does_not_invent_a_name():
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=FileNotFoundError)) as start:
        assert asyncio.run(read_codex_titles([])) == {}
        start.assert_not_called()
        assert asyncio.run(read_codex_titles([IDENTITY])) == {}
    assert run_reply({"id": 1, "result": {"thread": {"id": THREAD_ID, "name": None}}}) == {}
    assert run_reply({"id": 1, "error": {"code": -32600, "message": f"thread not loaded: {THREAD_ID}"}}) == {}


@pytest.mark.parametrize("response, error", [
    ({"id": 1, "result": {"thread": {"id": str(UUID("2" * 32)), "name": "另一聊天"}}}, "different thread"),
    ({"id": 7, "result": {}}, "different request"),
    ({"id": 1, "error": {"code": -32603, "message": "database damaged"}}, "database damaged"),
])
def test_wrong_identity_and_unknown_errors_are_not_hidden_as_missing_titles(response, error):
    with pytest.raises((ValueError, RuntimeError), match=error):
        run_reply(response)


def test_title_query_timeout_reclaims_its_own_process():
    process = SimpleNamespace(
        returncode=None, stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
        stdout=SimpleNamespace(readline=AsyncMock(side_effect=TimeoutError)),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")), wait=AsyncMock(), kill=Mock(),
    )
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        assert asyncio.run(read_codex_titles([IDENTITY])) == {}
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


@pytest.mark.process
def test_real_title_reader_batches_metadata_without_resuming_or_loading_turns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app-server").write_text(
        "import json, pathlib, sys\n"
        "assert sys.argv[1:] == ['--stdio']\n"
        "calls = []\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    calls.append(request)\n"
        "    pathlib.Path('calls.json').write_text(json.dumps(calls), encoding='utf-8')\n"
        "    if request['method'] == 'initialized': continue\n"
        "    if request['method'] == 'initialize': result = {}\n"
        "    else:\n"
        "        assert request['method'] == 'thread/read'\n"
        "        assert request['params']['includeTurns'] is False\n"
        "        result = {'thread': {'id': request['params']['threadId'], 'name': '原生标题'}}\n"
        "    print(json.dumps({'method': 'notification', 'params': {}}), flush=True)\n"
        "    print(json.dumps({'id': request['id'], 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    with patch("konden.assistant.host.codex_titles.default_codex_executable", return_value=sys.executable):
        assert asyncio.run(read_codex_titles([IDENTITY, "2" * 32, IDENTITY])) == {
            IDENTITY: "原生标题", "2" * 32: "原生标题",
        }
    calls = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))
    assert [item["method"] for item in calls] == ["initialize", "initialized", "thread/read", "thread/read"]
