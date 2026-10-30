"""Read local Codex display names without resuming chats or loading their history."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from konden.assistant.host.codex_continuation import default_codex_executable


class ThreadSummary(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str
    name: str | None = None


async def read_codex_titles(identities: list[str]) -> dict[str, str]:
    if not identities:
        return {}
    try:
        process = await asyncio.create_subprocess_exec(
            default_codex_executable(), "app-server", "--stdio",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        return {}
    stderr = asyncio.create_task(process.stderr.read())

    async def request(request_id, method, params):
        process.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode())
        await process.stdin.drain()
        while line := await process.stdout.readline():
            response = json.loads(line)
            if "id" not in response:
                continue
            if response["id"] != request_id:
                raise ValueError("Codex replied with a different request id")
            return response
        detail = (await stderr).decode("utf-8").strip()
        raise RuntimeError(detail or "Codex exited before replying to the title query")

    def result(response):
        if "error" in response:
            raise RuntimeError(f"Codex title query: {response['error']}")
        return response["result"]

    try:
        async with asyncio.timeout(5):
            result(await request(0, "initialize", {"clientInfo": {"name": "konden", "version": "0.1.0"}}))
            process.stdin.write(b'{"method":"initialized","params":{}}\n')
            await process.stdin.drain()
            titles = {}
            for request_id, identity in enumerate(dict.fromkeys(identities), 1):
                thread_id = str(UUID(identity))
                response = await request(request_id, "thread/read", {"threadId": thread_id, "includeTurns": False})
                if response.get("error") == {"code": -32600, "message": f"thread not loaded: {thread_id}"}:
                    continue
                thread = ThreadSummary.model_validate(result(response)["thread"])
                if thread.id != thread_id:
                    raise ValueError("Codex replied with a different thread id")
                if thread.name is not None and thread.name.strip():
                    titles[identity] = thread.name
            return titles
    except TimeoutError:
        # Optional local metadata may be unavailable; it never determines identity or delivery.
        return {}
    finally:
        process.stdin.close()
        if process.returncode is None:
            process.kill()
        await process.wait()
        await stderr
