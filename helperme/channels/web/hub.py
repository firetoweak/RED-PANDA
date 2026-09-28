from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebEvent:
    name: str
    data: dict


@dataclass(slots=True)
class _Preview:
    output_id: str
    text: str = ""


class WebEventHub:
    """Page-level fan-out. One live preview per Session; delivery never depends on browsers."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[WebEvent]] = set()
        self._previews: dict[str, _Preview] = {}
        self._thoughts: dict[str, _Preview] = {}

    def subscribe(self) -> asyncio.Queue[WebEvent]:
        queue: asyncio.Queue[WebEvent] = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[WebEvent]) -> None:
        self._queues.discard(queue)

    async def preview(
        self,
        session_id: str,
        phase: str,
        output_id: str,
        text: str | None,
    ) -> None:
        if phase == "started":
            self._previews[session_id] = _Preview(output_id)
            await self._broadcast(
                "preview.started",
                {"session_id": session_id, "output_id": output_id},
            )
            return
        if phase == "delta":
            preview = self._previews.get(session_id)
            if preview is None or preview.output_id != output_id:
                raise RuntimeError("preview output is not active")
            if type(text) is not str:
                raise TypeError("preview delta text must be str")
            preview.text += text
            await self._broadcast(
                "preview.delta",
                {
                    "session_id": session_id,
                    "output_id": output_id,
                    "text": text,
                },
            )
            return
        if phase == "aborted":
            preview = self._previews.pop(session_id, None)
            if preview is None:
                return
            if preview.output_id != output_id:
                raise RuntimeError("aborted output is not the active preview")
            await self._broadcast(
                "preview.aborted",
                {"session_id": session_id, "output_id": output_id},
            )
            return
        raise ValueError(f"unknown preview phase: {phase}")

    async def thinking(
        self,
        session_id: str,
        phase: str,
        output_id: str,
        text: str | None,
    ) -> None:
        if phase == "started":
            self._thoughts[session_id] = _Preview(output_id)
            await self._broadcast(
                "thinking.started",
                {"session_id": session_id, "output_id": output_id},
            )
            return
        if phase == "delta":
            thought = self._thoughts.get(session_id)
            if thought is None or thought.output_id != output_id:
                raise RuntimeError("thinking output is not active")
            if type(text) is not str:
                raise TypeError("thinking delta text must be str")
            thought.text += text
            await self._broadcast(
                "thinking.delta",
                {
                    "session_id": session_id,
                    "output_id": output_id,
                    "text": text,
                },
            )
            return
        if phase in {"finished", "aborted"}:
            thought = self._thoughts.pop(session_id, None)
            if thought is None:
                return
            if thought.output_id != output_id:
                raise RuntimeError(f"{phase} thinking is not the active stream")
            await self._broadcast(
                f"thinking.{phase}",
                {"session_id": session_id, "output_id": output_id},
            )
            return
        raise ValueError(f"unknown thinking phase: {phase}")

    async def output_final(self, session_id: str, output_id: str, text: str) -> None:
        preview = self._previews.get(session_id)
        if preview is not None and preview.output_id == output_id:
            del self._previews[session_id]
        await self._broadcast(
            "output_final",
            {
                "session_id": session_id,
                "output_id": output_id,
                "text": text,
            },
        )

    async def session_activity(self, session_id: str, activity: str) -> None:
        await self._broadcast(
            "session_activity",
            {"session_id": session_id, "activity": activity},
        )

    async def schedule_changed(self, session_id: str) -> None:
        await self._broadcast("schedule_changed", {"session_id": session_id})

    async def session_failed(self, session_id: str, message: str) -> None:
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if type(message) is not str or not message:
            raise ValueError("message must be a non-empty str")
        await self._broadcast(
            "session_failed",
            {"session_id": session_id, "message": message},
        )

    async def conversation_status(self, status) -> None:
        session_id = status.session_id
        compact_count = status.compact_count
        compact_phase = status.compact_phase
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if type(compact_count) is not int or compact_count < 0:
            raise ValueError("compact_count must be a nonnegative int")
        if compact_phase is not None and compact_phase not in {
            "running",
            "ready",
            "failed",
        }:
            raise ValueError("compact_phase must be running, ready, failed, or None")
        await self._broadcast(
            "conversation_status",
            {
                "session_id": session_id,
                "compact_count": compact_count,
                "compact_phase": compact_phase,
            },
        )

    async def context_usage(self, session_id: str, used: int, compact_threshold_tokens: int) -> None:
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if type(used) is not int or used < 0:
            raise ValueError("used must be a nonnegative int")
        if type(compact_threshold_tokens) is not int or compact_threshold_tokens <= 0:
            raise ValueError("compact_threshold_tokens must be a positive int")
        await self._broadcast(
            "context_usage",
            {"session_id": session_id, "used": used, "compact_threshold_tokens": compact_threshold_tokens},
        )

    async def tool_progress(
        self,
        session_id: str,
        phase: str,
        command_id: str,
        name: str,
        _payload,
    ) -> None:
        if phase not in {"start", "fail", "finish"}:
            raise ValueError(f"unknown tool progress phase: {phase}")
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if type(command_id) is not str or not command_id:
            raise ValueError("command_id must be a non-empty str")
        if type(name) is not str or not name:
            raise ValueError("name must be a non-empty str")
        await self._broadcast(
            "tool_progress",
            {
                "session_id": session_id,
                "command_id": command_id,
                "name": name,
                "status": "running" if phase == "start" else "settled",
            },
        )

    async def authorization_required(
        self,
        session_id: str,
        command_id: str,
        name: str,
        arguments: dict,
    ) -> None:
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if type(command_id) is not str or not command_id:
            raise ValueError("command_id must be a non-empty str")
        if type(name) is not str or not name:
            raise ValueError("name must be a non-empty str")
        await self._broadcast(
            "authorization_required",
            {
                "session_id": session_id,
                "command_id": command_id,
                "name": name,
                "arguments": arguments,
            },
        )

    async def _broadcast(self, name: str, data: dict) -> None:
        event = WebEvent(name, data)
        for queue in tuple(self._queues):
            queue.put_nowait(event)
