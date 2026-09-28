from __future__ import annotations

import json
import os
import asyncio
from inspect import isawaitable
from pathlib import Path
from uuid import uuid4

from helperme.config import AssistantConfig
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage, ToolCall


class ProcessLlm:
    def __init__(self, workspace):
        self.workspace = workspace

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def chat(self, messages, model, *, tools=None, on_content_delta=None, on_reasoning_delta=None):
        images = [
            part
            for message in messages
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part["type"] in ("image", "image_url")
        ]
        if images:
            (self.workspace / "received-images.json").write_text(
                json.dumps(images), encoding="utf-8"
            )
        text = json.dumps(messages)
        names = {tool["function"]["name"] for tool in tools}
        if "CRASH_PROCESS" in text:
            raise RuntimeError("intentional worker crash")
        if "BLOCK_PROCESS" in text or "report" in names:
            (self.workspace / f"blocked-{uuid4().hex}").touch()
            while not (self.workspace / "release").exists():
                await asyncio.sleep(0.02)
        calls = ()
        if "report" in names:
            if "READ_THEN_REPORT" in text and not any(
                m["role"] == "assistant" for m in messages
            ):
                calls = (ToolCall("read-one", "read_file", '{"path":"input.txt"}'),)
            else:
                calls = (ToolCall("report-one", "report", '{"summary":"child done"}'),)
        elif "BLOCK_TOOL" in text and not any(
            m["role"] == "assistant" for m in messages
        ):
            calls = (ToolCall("read-one", "read_file", '{"path":"input.txt"}'),)
        elif "DELEGATE_CHILDREN" in text and not any(
            m["role"] == "assistant" for m in messages
        ):
            calls = tuple(
                ToolCall(f"delegate-{i}", "delegate", '{"task":"read something"}')
                for i in range(2)
            )
        content = "" if calls else "done"
        if content and on_content_delta is not None:
            emitted = on_content_delta(content)
            if isawaitable(emitted):
                await emitted
        return LLMCallResult(
            LLMResponse(content=content, calls=calls),
            LLMUsage(input_tokens=10, output_tokens=5),
        )


def config_for(workspace: Path):
    return AssistantConfig(
        model_name="test",
        compact_threshold_tokens=200000,
        llm=ProcessLlm(workspace),
    )


class CancellableProcessLlm(ProcessLlm):
    async def chat(self, messages, model, *, tools=None, on_content_delta=None, on_reasoning_delta=None):
        if "CANCEL_PROCESS" not in json.dumps(messages):
            return await super().chat(
                messages,
                model,
                tools=tools,
                on_content_delta=on_content_delta,
            )
        (self.workspace / "cancel-started").touch()
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            (self.workspace / "cancel-observed").touch()


def cancellable_config(workspace: Path):
    config = config_for(workspace)
    return AssistantConfig(
        model_name=config.model_name,
        compact_threshold_tokens=config.compact_threshold_tokens,
        llm=CancellableProcessLlm(workspace),
    )


def interrupted_read_config(workspace: Path):
    from helperme.assistant.host import worker
    from helperme.runtime import ToolBinding

    build = worker.build_assistant_assembly

    async def read(context, arguments):
        marker = workspace / "read-started"
        if not marker.exists():
            marker.touch()
            # Kill without a Python except path; recovery must report the interruption.
            os._exit(1)
        (workspace / "read-retried").touch()
        return {"ok": True, "code": "FILE_READ", "data": "read successfully"}

    async def assembly(*args, **kwargs):
        result = await build(*args, **kwargs)
        result.runtime.bind_tool("read_file", ToolBinding(read))
        return result

    worker.build_assistant_assembly = assembly
    return config_for(workspace)


def blocking_tool_config(workspace: Path):
    from helperme.assistant.host import worker
    from helperme.assistant.assembly import _with_tool_progress
    from helperme.runtime import ToolBinding

    build = worker.build_assistant_assembly

    async def read(context, arguments):
        (workspace / "tool-started").touch()
        while not (workspace / "release-tool").exists():
            await asyncio.sleep(0.02)
        return {"ok": True, "code": "FILE_READ", "data": "done"}

    async def assembly(*args, **kwargs):
        result = await build(*args, **kwargs)
        binding = _with_tool_progress(
            {"read_file": ToolBinding(read)},
            kwargs["tool_progress_sink"],
        )["read_file"]
        result.runtime.bind_tool("read_file", binding)
        return result

    worker.build_assistant_assembly = assembly
    return config_for(workspace)


def failing_startup_config(workspace: Path, stage: str):
    """Fail once so the parent can subsequently start with the same factory."""
    from helperme.assistant.host import worker

    marker = workspace / f"failed-{stage}"
    if marker.exists():
        return config_for(workspace)
    marker.touch()
    if stage == "config":
        raise RuntimeError("config initialization failed")
    if stage == "assembly":

        async def fail(*args, **kwargs):
            raise RuntimeError("assembly initialization failed")

        worker.build_assistant_assembly = fail
    config = config_for(workspace)
    if stage == "client":
        from helperme.mcp.client_manager import McpClientManager

        async def enter(self):
            raise RuntimeError("client initialization failed")

        McpClientManager.__aenter__ = enter
    return config


def failing_request_config(workspace: Path):
    from helperme.assistant.host import worker

    build = worker.build_assistant_assembly

    async def assembly(*args, **kwargs):
        result = await build(*args, **kwargs)

        async def fail(session_id, **kwargs):
            raise RuntimeError("application request failed")

        result.sessions.resolve_authorizations = fail
        return result

    worker.build_assistant_assembly = assembly
    return config_for(workspace)


def delegate_startup_failure_config(workspace: Path):
    import multiprocessing

    if "/sub-" in multiprocessing.current_process().name:
        raise RuntimeError("child initialization failed")
    return config_for(workspace)
