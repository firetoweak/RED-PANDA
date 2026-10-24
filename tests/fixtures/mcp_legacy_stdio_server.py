from __future__ import annotations

import json
import sys


def respond(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "server/discover":
        respond(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )
    elif method == "initialize":
        respond(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "konden-legacy-test",
                        "version": "1.0.0",
                    },
                },
            }
        )
    elif method == "tools/list":
        tools = ([{"name": "read_client_roots", "inputSchema": {"type": "object", "properties": {}}}]
                 if "--roots" in sys.argv else [])
        respond(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": tools},
            }
        )
    elif method == "tools/call" and "--roots" in sys.argv:
        respond({"jsonrpc": "2.0", "id": "roots-request", "method": "roots/list", "params": {}})
        roots_reply = json.loads(sys.stdin.readline())
        assert roots_reply["id"] == "roots-request"
        roots = {"roots": [item["uri"] for item in roots_reply["result"]["roots"]]}
        respond({
            "jsonrpc": "2.0", "id": request_id,
            "result": {"content": [{"type": "text", "text": json.dumps(roots)}], "structuredContent": roots},
        })
    else:
        respond(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )
