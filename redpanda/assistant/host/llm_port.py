"""Host-owned LLM implementation reached from a Worker through the existing pipe."""

from __future__ import annotations

from inspect import isawaitable

from redpanda.llm.api import (
    decode_llm_error,
    decode_llm_result,
    encode_images,
    encode_llm_error,
    encode_llm_result,
)


class WorkerLlmPort:
    def __init__(self, peer, session_id: str) -> None:
        self._peer = peer
        self._session_id = session_id
        self._read_attachment = None

    def bind_attachment_reader(self, read) -> None:
        self._read_attachment = read

    async def __aenter__(self) -> "WorkerLlmPort":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def chat(
        self,
        messages,
        model,
        tools=None,
        *,
        on_content_delta=None,
        on_reasoning_delta=None,
        reasoning_effort=None,
    ):
        request_messages = encode_images(messages, self._read_attachment)

        async def on_delta(text: str) -> None:
            if on_content_delta is None:
                return
            emitted = on_content_delta(text)
            if isawaitable(emitted):
                await emitted

        async def on_reasoning(text: str) -> None:
            if on_reasoning_delta is None:
                return
            emitted = on_reasoning_delta(text)
            if isawaitable(emitted):
                await emitted

        payload = await self._peer.request(
            "llm_chat",
            self._session_id,
            {
                "messages": request_messages,
                "model": model,
                "tools": tools,
                "stream": on_content_delta is not None,
                "stream_reasoning": on_reasoning_delta is not None,
                "reasoning_effort": reasoning_effort,
            },
            on_delta=on_delta if on_content_delta is not None else None,
            on_reasoning_delta=(
                on_reasoning if on_reasoning_delta is not None else None
            ),
        )
        if payload["ok"]:
            return decode_llm_result(payload["result"])
        raise decode_llm_error(payload["error"])


async def complete_llm_chat(llm, arguments, on_delta, on_reasoning_delta=None):
    options = {} if arguments["reasoning_effort"] is None else {"reasoning_effort": arguments["reasoning_effort"]}
    try:
        result = await llm.chat(
            arguments["messages"],
            arguments["model"],
            tools=arguments["tools"],
            on_content_delta=on_delta if arguments["stream"] else None,
            on_reasoning_delta=(
                on_reasoning_delta if arguments.get("stream_reasoning") else None
            ),
            **options,
        )
    except Exception as error:
        return {"ok": False, "error": encode_llm_error(error)}
    return {"ok": True, "result": encode_llm_result(result)}
