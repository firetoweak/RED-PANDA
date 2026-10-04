"""Manual ACP v1 stdio probe for Obsidian Agent Client.

Send ``hello`` to verify the prompt lifecycle. Send ``wait`` and then use
Obsidian's cancel action to verify ``session/cancel``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import gettempdir
from threading import Event, Lock, Thread
from time import time
from uuid import uuid4


LOG_PATH = Path(
    os.environ.get(
        "REDPANDA_ACP_PROBE_LOG",
        str(Path(gettempdir()) / "redpanda-acp-v1-probe.ndjson"),
    )
)

_output_lock = Lock()
_log_lock = Lock()
_state_lock = Lock()
_sessions: set[str] = set()
_active: dict[str, Event] = {}


def _record(direction: str, payload: object) -> None:
    entry = {
        "time": time(),
        "pid": os.getpid(),
        "direction": direction,
        "payload": payload,
    }
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _send(payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _output_lock:
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()
    _record("out", payload)


def _result(request_id: object, result: dict[str, object]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: object, code: int, message: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _text_from_prompt(prompt: object) -> str:
    if not isinstance(prompt, list):
        raise ValueError("prompt must be an array")
    parts: list[str] = []
    for block in prompt:
        if not isinstance(block, dict):
            raise ValueError("prompt blocks must be objects")
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("text block must contain string text")
            parts.append(text)
    return "\n".join(parts)


def _update(session_id: str, text: str, message_id: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                    "messageId": message_id,
                },
            },
        }
    )


def _run_prompt(
    request_id: object,
    session_id: str,
    text: str,
    cancelled: Event,
) -> None:
    message_id = f"probe-message-{uuid4().hex}"
    try:
        if text.strip().lower() == "wait":
            _update(session_id, "探针正在等待；现在请点击取消。", message_id)
            if cancelled.wait(30):
                _result(request_id, {"stopReason": "cancelled"})
            else:
                _update(session_id, "等待结束，未收到取消。", message_id)
                _result(request_id, {"stopReason": "end_turn"})
        else:
            _update(session_id, f"probe: {text}", message_id)
            _result(request_id, {"stopReason": "end_turn"})
    finally:
        with _state_lock:
            current = _active.get(session_id)
            if current is cancelled:
                del _active[session_id]


def _handle_request(message: dict[str, object]) -> None:
    method = message.get("method")
    params = message.get("params", {})
    request_id = message.get("id")
    if not isinstance(method, str) or not isinstance(params, dict):
        if "id" in message:
            _error(request_id, -32600, "Invalid Request")
        return

    if method == "initialize":
        if params.get("protocolVersion") != 1:
            _error(request_id, -32602, "Only ACP protocol version 1 is supported")
            return
        _result(
            request_id,
            {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": False,
                    "promptCapabilities": {
                        "image": False,
                        "audio": False,
                        "embeddedContext": False,
                    },
                },
                "authMethods": [],
                "agentInfo": {
                    "name": "redpanda-acp-probe",
                    "title": "RED PANDA ACP Probe",
                    "version": "0.1.0",
                },
            },
        )
        return

    if method == "session/new":
        cwd = params.get("cwd")
        mcp_servers = params.get("mcpServers")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            _error(request_id, -32602, "cwd must be an absolute path")
            return
        if not isinstance(mcp_servers, list):
            _error(request_id, -32602, "mcpServers must be an array")
            return
        session_id = f"probe-session-{uuid4().hex}"
        with _state_lock:
            _sessions.add(session_id)
        _result(request_id, {"sessionId": session_id})
        return

    if method == "session/prompt":
        session_id = params.get("sessionId")
        if not isinstance(session_id, str):
            _error(request_id, -32602, "sessionId must be a string")
            return
        try:
            text = _text_from_prompt(params.get("prompt"))
        except ValueError as error:
            _error(request_id, -32602, str(error))
            return
        with _state_lock:
            if session_id not in _sessions:
                _error(request_id, -32602, "Unknown session")
                return
            if session_id in _active:
                _error(request_id, -32000, "Session already has an active prompt")
                return
            cancelled = Event()
            _active[session_id] = cancelled
        Thread(
            target=_run_prompt,
            args=(request_id, session_id, text, cancelled),
            daemon=True,
        ).start()
        return

    if method == "session/cancel":
        session_id = params.get("sessionId")
        if isinstance(session_id, str):
            with _state_lock:
                cancelled = _active.get(session_id)
            if cancelled is not None:
                cancelled.set()
        return

    if "id" in message:
        _error(request_id, -32601, f"Method not found: {method}")


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _record("start", {"protocolVersion": 1, "logPath": str(LOG_PATH)})
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _record("invalid", line)
            _error(None, -32700, "Parse error")
            continue
        _record("in", message)
        if not isinstance(message, dict):
            _error(None, -32600, "Invalid Request")
            continue
        _handle_request(message)
    with _state_lock:
        active = tuple(_active.values())
    for cancelled in active:
        cancelled.set()
    _record("stop", {})


if __name__ == "__main__":
    main()
