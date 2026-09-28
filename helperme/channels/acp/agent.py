from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from acp import PROTOCOL_VERSION, RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    TextContentBlock,
)

from helperme.channels.acp.project import (
    tool_failed_from_result,
    tool_finish,
    tool_start,
)


@dataclass(slots=True)
class _ActivePrompt:
    accepted: asyncio.Event
    cancel_requested: asyncio.Event
    cancellation_done: asyncio.Future[None]
    cancellation_started: bool = False


class HelperMeAcpAgent:
    """ACP v1 stdio boundary over HelperMe's Session application service."""

    def __init__(self, sessions, workspaces) -> None:
        self._sessions = sessions
        self._workspaces = workspaces
        self._client = None
        self._connection_id = uuid4().hex
        self._owners: dict[str, str] = {}
        self._active: dict[str, _ActivePrompt] = {}
        self._previews: dict[tuple[str, str], str] = {}
        self._delivered: dict[tuple[str, str], str] = {}

    def on_connect(self, client) -> None:
        self._client = client

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities=None,
        client_info=None,
        **_kwargs,
    ) -> InitializeResponse:
        if protocol_version != PROTOCOL_VERSION:
            raise RequestError.invalid_params(
                {"protocolVersion": f"only {PROTOCOL_VERSION} is supported"}
            )
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=False,
                prompt_capabilities=PromptCapabilities(
                    image=False,
                    audio=False,
                    embedded_context=False,
                ),
            ),
            auth_methods=[],
            agent_info=Implementation(
                name="helperme",
                title="HelperMe",
                version="0.1.0",
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories=None,
        mcp_servers=None,
        **_kwargs,
    ) -> NewSessionResponse:
        workspace = self._workspace_for(cwd)
        if additional_directories:
            raise RequestError.invalid_params(
                {"additionalDirectories": "not supported"}
            )
        if mcp_servers:
            raise RequestError.invalid_params({"mcpServers": "not supported"})
        session_id = f"session-{uuid4().hex}"
        owner = f"acp-{self._connection_id}-session-{session_id}"
        await self._sessions.create(session_id, workspace.workspace_id)
        await self._sessions.select(owner, session_id)
        self._owners[session_id] = owner
        return NewSessionResponse(session_id=session_id)

    async def prompt(
        self,
        session_id: str,
        prompt: list,
        **_kwargs,
    ) -> PromptResponse:
        if session_id not in self._owners:
            raise RequestError(-32002, "Session not found", {"sessionId": session_id})
        if session_id in self._active:
            raise RequestError(-32000, "Session already has an active prompt")
        if not prompt or any(type(block) is not TextContentBlock for block in prompt):
            raise RequestError.invalid_params({"prompt": "only text is supported"})
        content = "\n".join(block.text for block in prompt)
        loop = asyncio.get_running_loop()
        turn = _ActivePrompt(
            accepted=asyncio.Event(),
            cancel_requested=asyncio.Event(),
            cancellation_done=loop.create_future(),
        )
        self._active[session_id] = turn
        quiescent = None
        cancelled = None
        try:
            view = await self._sessions.accept_input(
                session_id,
                content,
                source="acp",
                delivery_id=f"acp-prompt-{uuid4().hex}",
            )
            turn.accepted.set()
            if view.control_approval is not None:
                await self.deliver(
                    session_id,
                    f"control-{uuid4().hex}",
                    (
                        f"{view.control_approval.summary}\n"
                        f"风险：{view.control_approval.risk}\n"
                        "输入 yes 确认，no 取消。"
                    ),
                )
            elif view.control_message is not None:
                await self.deliver(
                    session_id,
                    f"control-{uuid4().hex}",
                    view.control_message,
                )
            quiescent = asyncio.create_task(
                self._sessions.wait_quiescent(session_id)
            )
            cancelled = asyncio.create_task(turn.cancel_requested.wait())
            await asyncio.wait(
                (quiescent, cancelled),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if turn.cancel_requested.is_set():
                await turn.cancellation_done
                return PromptResponse(stop_reason="cancelled")
            await quiescent
            return PromptResponse(stop_reason="end_turn")
        finally:
            turn.accepted.set()
            for task in (quiescent, cancelled):
                if task is not None and not task.done():
                    task.cancel()
            pending = tuple(
                task
                for task in (quiescent, cancelled)
                if task is not None
            )
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self._active.get(session_id) is turn:
                del self._active[session_id]

    async def cancel(self, session_id: str, **_kwargs) -> None:
        turn = self._active.get(session_id)
        if turn is None:
            return
        turn.cancel_requested.set()
        await turn.accepted.wait()
        if turn.cancellation_started:
            await turn.cancellation_done
            return
        turn.cancellation_started = True
        try:
            await self._sessions.cancel_turn(session_id)
        except BaseException as error:
            turn.cancellation_done.set_exception(error)
            raise
        turn.cancellation_done.set_result(None)

    async def deliver(self, session_id: str, output_id: str, text: str) -> None:
        turn = self._active.get(session_id)
        if turn is None or turn.cancel_requested.is_set():
            return
        key = (session_id, output_id)
        if key in self._delivered:
            if self._delivered[key] != text:
                raise RuntimeError("output_id was delivered with different text")
            return
        if key in self._previews:
            streamed = self._previews.pop(key)
            if streamed.strip() != text:
                raise RuntimeError("committed output differs from its preview")
            self._delivered[key] = text
            return
        await self._send_message_chunk(session_id, output_id, text)
        self._delivered[key] = text

    async def preview(
        self,
        session_id: str,
        phase: str,
        output_id: str,
        text: str | None,
    ) -> None:
        turn = self._active.get(session_id)
        if turn is None or turn.cancel_requested.is_set():
            return
        key = (session_id, output_id)
        if phase == "started":
            self._previews[key] = ""
            return
        if phase == "delta":
            self._previews[key] += text
            await self._send_message_chunk(session_id, output_id, text)
            return
        if phase == "aborted":
            self._previews.pop(key)
            return
        raise ValueError(f"unknown preview phase: {phase}")

    async def _send_message_chunk(
        self,
        session_id: str,
        output_id: str,
        text: str,
    ) -> None:
        await self._client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=text),
                message_id=f"message-{output_id}",
            ),
        )

    async def report_tool(
        self,
        session_id: str,
        phase: str,
        command_id: str,
        name: str,
        payload,
    ) -> None:
        if self._client is None or session_id not in self._owners:
            return
        if phase == "start":
            update = tool_start(command_id, name, payload)
        elif phase == "fail":
            update = tool_finish(command_id, payload, failed=True)
        elif phase == "finish":
            update = tool_finish(
                command_id,
                payload,
                failed=tool_failed_from_result(payload),
            )
        else:
            raise ValueError(f"unknown tool progress phase: {phase}")
        await self._client.session_update(session_id=session_id, update=update)

    async def close(self) -> None:
        for owner in tuple(self._owners.values()):
            await self._sessions.release(owner)
        self._owners.clear()
        self._previews.clear()
        self._delivered.clear()

    def _workspace_for(self, cwd: str):
        requested = Path(cwd)
        if not requested.is_absolute() or not requested.is_dir():
            raise RequestError.invalid_params(
                {"cwd": "must be an existing absolute directory"}
            )
        return self._workspaces.register_path(requested)
