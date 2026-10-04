from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from inspect import isawaitable
from typing import Literal

from redpanda.runtime.dispatcher import AttemptContext, ToolBinding
from redpanda.runtime.model import (
    InvokeTool,
    ModelDecision,
)


DELIVER_TOOL_NAME = "deliver"


DeliverySink = Callable[[str, str, str], Awaitable[None] | None]
"""Route one Session's output. The Host decides where each Session lands."""

PreviewPhase = Literal["started", "delta", "aborted"]
PreviewSink = Callable[
    [str, PreviewPhase, str, str | None],
    Awaitable[None] | None,
]
ThinkingPhase = Literal["started", "delta", "finished", "aborted"]
ThinkingSink = Callable[
    [str, ThinkingPhase, str, str | None],
    Awaitable[None] | None,
]


@dataclass(slots=True)
class PreviewEmitter:
    """One disposable preview per Session; committed text still uses deliver.

    Thinking is a parallel, Channel-only stream. It is not deliver and does
    not become assistant text.
    """

    sink: PreviewSink | None = None
    thinking_sink: ThinkingSink | None = None
    _active: dict[str, str] = field(default_factory=dict, init=False)
    _thinking: dict[str, str] = field(default_factory=dict, init=False)

    @property
    def enabled(self) -> bool:
        return self.sink is not None

    async def start(self, session_id: str, output_id: str) -> None:
        if self.sink is None:
            return
        self._active[session_id] = output_id
        await _emit_preview(self.sink, session_id, "started", output_id, None)

    async def append(self, session_id: str, output_id: str, text: str) -> None:
        if self.sink is None:
            return
        if self._active[session_id] != output_id:
            raise RuntimeError("preview output is not active")
        await _emit_preview(self.sink, session_id, "delta", output_id, text)

    async def abort(self, session_id: str) -> None:
        if self.sink is None:
            return
        output_id = self._active.pop(session_id, None)
        if output_id is not None:
            await _emit_preview(
                self.sink, session_id, "aborted", output_id, None
            )

    def finish(self, session_id: str, output_id: str) -> None:
        """Retire this output's preview, if it still owns the stream.

        deliver 在 Step 之外执行，而它的 decision_on_outcome 为 false，
        不拦续跑。所以交付回到这里时，预览槽位可能已经被下一次决策接管；
        那条旧预览早就被顶掉了，这里没有东西要收。
        """

        if self.sink is None:
            return
        if self._active.get(session_id) == output_id:
            del self._active[session_id]

    async def start_thinking(self, session_id: str, output_id: str) -> None:
        if self.thinking_sink is None or session_id in self._thinking:
            return
        self._thinking[session_id] = output_id
        await _emit_thinking(
            self.thinking_sink, session_id, "started", output_id, None
        )

    async def append_thinking(
        self, session_id: str, output_id: str, text: str
    ) -> None:
        if self.thinking_sink is None:
            return
        if self._thinking.get(session_id) != output_id:
            raise RuntimeError("thinking output is not active")
        await _emit_thinking(
            self.thinking_sink, session_id, "delta", output_id, text
        )

    async def finish_thinking(self, session_id: str, output_id: str) -> None:
        if self.thinking_sink is None:
            return
        active = self._thinking.pop(session_id, None)
        if active is None:
            return
        if active != output_id:
            raise RuntimeError("finished thinking is not the active stream")
        await _emit_thinking(
            self.thinking_sink, session_id, "finished", output_id, None
        )

    async def abort_thinking(self, session_id: str) -> None:
        if self.thinking_sink is None:
            return
        output_id = self._thinking.pop(session_id, None)
        if output_id is not None:
            await _emit_thinking(
                self.thinking_sink, session_id, "aborted", output_id, None
            )


async def _emit_preview(
    sink: PreviewSink,
    session_id: str,
    phase: PreviewPhase,
    output_id: str,
    text: str | None,
) -> None:
    emitted = sink(session_id, phase, output_id, text)
    if isawaitable(emitted):
        await emitted


async def _emit_thinking(
    sink: ThinkingSink,
    session_id: str,
    phase: ThinkingPhase,
    output_id: str,
    text: str | None,
) -> None:
    emitted = sink(session_id, phase, output_id, text)
    if isawaitable(emitted):
        await emitted


def ensure_deliver(decision: ModelDecision, output_id: str) -> ModelDecision:
    """Map assistant text onto an Assistant-owned deliver Command.

    `deliver` is not a model-visible tool. Runtime does not promote
    `decision.content` into a user-visible delivery.
    """

    if any(
        isinstance(request, InvokeTool) and request.name == DELIVER_TOOL_NAME
        for request in decision.command_requests
    ):
        raise ValueError("deliver is a product command, not a model tool")
    if type(output_id) is not str or not output_id:
        raise ValueError("output_id must be a non-empty str")
    text = decision.content.strip()
    if not text:
        return decision
    return replace(
        decision,
        command_requests=decision.command_requests
        + (
            InvokeTool(
                DELIVER_TOOL_NAME,
                (("output_id", output_id), ("text", text)),
            ),
        ),
    )


async def emit_delivery(
    sink: DeliverySink,
    session_id: str,
    output_id: str,
    text: str,
) -> None:
    """把助手正文交给 Channel。`output_id` 必须是该 Step 的 trigger event id。"""
    emitted = sink(session_id, output_id, text)
    if isawaitable(emitted):
        await emitted


def deliver_binding(
    sink: DeliverySink,
    preview: PreviewEmitter | None = None,
) -> dict[str, ToolBinding]:
    async def handler(
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> str:
        output_id = arguments.get("output_id")
        if type(output_id) is not str or not output_id:
            raise ValueError("deliver output_id must be a non-empty str")
        text = arguments.get("text")
        if type(text) is not str or not text:
            raise ValueError("deliver text must be a non-empty str")
        await emit_delivery(sink, context.session_id, output_id, text)
        if preview is not None:
            preview.finish(context.session_id, output_id)
        return text

    return {
        DELIVER_TOOL_NAME: ToolBinding(
            handler,
            decision_on_outcome=False,
        ),
    }
