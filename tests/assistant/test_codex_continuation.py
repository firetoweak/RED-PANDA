"""The local CLI boundary must preserve fixed text and distinguish uncertain delivery."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from konden.assistant.host.codex_continuation import CodexTarget, WAKE_MESSAGE, queue_codex


def test_queue_failure_and_timeout_do_not_claim_success():
    target = CodexTarget(thread_id="01234567-89ab-cdef-0123-456789abcdef", executable="codex")
    process = SimpleNamespace(returncode=2, communicate=AsyncMock(return_value=(b"", b"thread is unavailable")))
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        result = asyncio.run(queue_codex(target, "message-1"))
    assert result.status == "failed" and result.detail == "thread is unavailable"
    process = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=TimeoutError),
                              kill=Mock(), wait=AsyncMock())
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
        result = asyncio.run(queue_codex(target, "message-2"))
    assert result.status == "unknown" and "无法确认" in result.detail
    process.kill.assert_called_once()


@pytest.mark.process
def test_real_queue_process_receives_exact_thread_and_fixed_unicode_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "queue").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path('received.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    target = CodexTarget(thread_id="01234567-89ab-cdef-0123-456789abcdef", executable=sys.executable)
    result = asyncio.run(queue_codex(target, "message-1"))
    assert result.status == "queued"
    assert json.loads((tmp_path / "received.json").read_text(encoding="utf-8")) == [
        "--thread", target.thread_id, "--message", WAKE_MESSAGE,
    ]
