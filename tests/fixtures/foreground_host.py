"""A real Host whose model observes command results before deciding its next action."""
from __future__ import annotations

import asyncio
import json
import sys
from functools import partial
from pathlib import Path

from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.host.supervisor import HostSupervisor
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.llm.api import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from redpanda.paths import RedPandaHome
from tests.fixtures.session_worker import config_for


class ObservingCommandLlm:
    def __init__(self, root):
        self.root = root

    async def chat(self, messages, model, **kwargs):
        with (self.root / "model-calls").open("a") as stream:
            stream.write("call\n")
        background = (self.root / "background-mode").exists()
        if any(message["role"] == "tool" for message in messages):
            (self.root / "observed.json").write_text(json.dumps(messages), encoding="utf-8")
            (self.root / "handoff").touch()
            return LLMCallResult(LLMResponse(content="done"), LLMUsage(input_tokens=10, output_tokens=5))
        command = (self.root / "command").read_text(encoding="utf-8")
        return LLMCallResult(LLMResponse(calls=(ToolCall(
            "repeat", "execute_command", json.dumps({"command": command, "wait_seconds": 0 if background else 30}),
        ),)), LLMUsage(input_tokens=10, output_tokens=5))


async def main(root):
    home = RedPandaHome(root / "home")
    home.initialize()
    registry = WorkspaceRegistry.load(home.workspaces_path)
    store = SessionStore(home.runtime_sessions_root)
    host = HostSupervisor(store, partial(config_for, root), home, None, llm=ObservingCommandLlm(root))
    try:
        if not store.path("session").is_file():
            workspace = registry.register_path(root)
            # Host loaded the same empty registry before initial registration.
            host.workspaces = registry
            await host.create("session", workspace.workspace_id)
            await host.set_auto_authorize(True)
            await host.receive_user_message("session", "run the command", delivery_id="run")
        else:
            await host.resume_unfinished_sessions()
        (root / "ready").touch()
        while not (root / "close-host").exists():
            await asyncio.sleep(.02)
    finally:
        await host.close()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
