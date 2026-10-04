from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from redpanda.assistant.control import CONTROL_SOURCE, AssistantControlPlane
from redpanda.assistant.delivery import PreviewEmitter
from redpanda.assistant.failures import assistant_failure_message
from redpanda.assistant.management import ManagementSurface
from redpanda.assistant.toolsets import ToolSurface
from redpanda.assistant.catalog import CapabilityCatalog
from redpanda.runtime import (
    AgentRuntime,
    CommandOutcome,
    DispatchAttemptStarted,
    OutcomeStatus,
    RuntimeStatus,
)
from redpanda.runtime.model import CanonicalState
from redpanda.tools.builtin import CommandInterrupts, LiveCommand


class SessionNotFoundError(LookupError):
    pass


async def resume_session(
    runtime: AgentRuntime,
    surface: ToolSurface,
    session_id: str,
    management: ManagementSurface,
    catalog: CapabilityCatalog,
) -> CanonicalState:
    """Select an existing Session and rebuild Host projections."""

    if not await runtime.session_exists(session_id):
        raise SessionNotFoundError(session_id)
    events = await runtime.snapshot(session_id)
    state = await runtime.state(session_id)
    if state.waiting_command_ids:
        catalog.rehydrate(session_id, events)
    else:
        await catalog.sync(runtime, session_id)
    await surface.rehydrate(session_id, events)
    await management.rehydrate(session_id, events)
    return await runtime.state(session_id)


class SessionScheduler:
    """Advance one bound Session from committed facts."""

    def __init__(
        self,
        runtime: AgentRuntime,
        session_id: str,
        *,
        control: AssistantControlPlane,
        on_quiesced: (
            Callable[[str, CanonicalState], Awaitable[None] | None] | None
        ) = None,
        on_failed: Callable[[str, str], Awaitable[None] | None] | None = None,
        session_failed: Callable[[str, str], Awaitable[None] | None] | None = None,
        preview: PreviewEmitter | None = None,
    ) -> None:
        self._runtime = runtime
        self._session_id = session_id
        self._control = control
        self._on_quiesced = on_quiesced
        self._on_failed = on_failed
        self._session_failed = session_failed
        self._preview = PreviewEmitter() if preview is None else preview
        self.before_advance = None
        self.record_workspace_versions = None
        self.auto_authorize = None
        self.authorization_required = None
        self.command_interrupts: CommandInterrupts | None = None
        self.propagate_failures = False
        self._task: asyncio.Task[bool] | None = None
        self._pending_wake = False
        self._failure: BaseException | None = None
        self._failure_event = asyncio.Event()
        self.changed = asyncio.Event()
        runtime.dispatcher.connect(self.wake, self._record_failure)

    async def wake(self, session_id: str) -> None:
        assert session_id == self._session_id
        self.changed.set()
        if self._failure is not None:
            raise self._failure
        task = self._task
        # The completion callback owns the slot until it has observed the result.
        if task is not None:
            self._pending_wake = True
            return
        self._start()

    def _start(self) -> None:
        task = asyncio.create_task(
            self._advance_once(),
            name=f"agent-session:{self._session_id}",
        )
        self._task = task
        task.add_done_callback(self._task_done)

    async def _advance_once(self) -> bool:
        session_id = self._session_id
        if self.record_workspace_versions is not None:
            await self.record_workspace_versions()
        events = await self._runtime.snapshot(session_id)
        position = events[-1].sequence if events else 0
        if self.before_advance is not None and not await self.before_advance():
            return False
        try:
            advance = await self._runtime.advance(
                session_id, expected_journal_position=position
            )
        except Exception as error:
            try:
                await self._preview.abort(session_id)
                await self._preview.abort_thinking(session_id)
            except BaseException as preview_error:
                raise BaseExceptionGroup(
                    "session advance and preview cleanup failed",
                    [error, preview_error],
                ) from None
            if self.propagate_failures:
                raise
            message = assistant_failure_message(error)
            if message is None:
                raise
            # 已识别的模型失败只停这条 Session：它仍是 RUNNABLE，
            # 下一条外部事实会从同一个 trigger 重试。失败是这次推进的
            # 瞬时状态，不走 deliver，避免被 Channel 当成助手回复留下。
            await self._emit_session_failed(session_id, f"运行失败：{message}")
            await self._failed(session_id, message)
            return False
        if self.record_workspace_versions is not None:
            await self.record_workspace_versions()
        if advance.step is None and advance.status is not RuntimeStatus.RUNNABLE:
            await self._preview.abort(session_id)
            await self._preview.abort_thinking(session_id)
        runnable = advance.status is RuntimeStatus.RUNNABLE
        # Control 请求随 Step 提交；这里从 Journal 恢复，而不是依赖本轮内存。
        outcome = await self._control.prepare_pending(
            await self._runtime.snapshot(session_id)
        )
        if outcome is not None:
            await self._runtime.receive_domain_fact(
                session_id,
                outcome.fact_type,
                dict(outcome.data),
                delivery_id=outcome.delivery_id,
                source=CONTROL_SOURCE,
                requests_decision=outcome.requests_decision,
            )
            runnable = runnable or outcome.requests_decision
        if not runnable:
            await self._quiesced(session_id)
        return runnable

    async def _emit_session_failed(self, session_id: str, message: str) -> None:
        if self._session_failed is None:
            return
        observed = self._session_failed(session_id, message)
        if isinstance(observed, Awaitable):
            await observed

    async def _quiesced(self, session_id: str) -> None:
        """本次推进没有留下待办。订阅者自己判断这是否算一件事做完了。"""

        if (
            self.auto_authorize is None
            and self.authorization_required is None
            and self._on_quiesced is None
        ):
            return
        state = await self._runtime.state(session_id)
        pending = pending_authorization_ids(state)
        if pending:
            should_auto = False
            if self.auto_authorize is not None:
                should_auto = self.auto_authorize(session_id)
                if isinstance(should_auto, Awaitable):
                    should_auto = await should_auto
            if should_auto:
                for command_id in pending:
                    await self._runtime.grant_command(session_id, command_id)
                await self.wake(session_id)
                return
            if self.authorization_required is not None:
                by_id = {
                    command_state.command.command_id: command_state
                    for command_state in state.commands
                }
                for command_id in pending:
                    command_state = by_id.get(command_id)
                    if command_state is None:
                        continue
                    effect = command_state.command.effect
                    observed = self.authorization_required(
                        session_id,
                        command_id,
                        effect.name,
                        effect.argument_dict(),
                    )
                    if isinstance(observed, Awaitable):
                        await observed
        if self._on_quiesced is None:
            return
        observed = self._on_quiesced(session_id, state)
        if isinstance(observed, Awaitable):
            await observed

    async def _failed(self, session_id: str, message: str) -> None:
        """推进因已识别的失败停下。与静止是两件事，订阅者分开处理。"""

        if self._on_failed is None:
            return
        observed = self._on_failed(session_id, message)
        if isinstance(observed, Awaitable):
            await observed

    def _task_done(self, task: asyncio.Task[bool]) -> None:
        self.changed.set()
        self._task = None
        pending_wake = self._pending_wake
        self._pending_wake = False
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._record_failure(error)
        elif self._failure is None and (task.result() or pending_wake):
            self._start()

    def _record_failure(self, error: BaseException) -> None:
        self.changed.set()
        if self._failure is None:
            self._failure = error
            self._failure_event.set()

    async def wait_failure(self) -> BaseException:
        await self._failure_event.wait()
        assert self._failure is not None
        return self._failure

    @property
    def idle(self) -> bool:
        return self._task is None and self._runtime.dispatcher.active_count == 0

    async def cancel_turn(self, session_id: str) -> None:
        assert session_id == self._session_id
        interrupts = self.command_interrupts
        await self._runtime.cancel_turn(session_id)
        if interrupts is not None:
            interrupted, errors = await interrupts.wait(interrupts.signal())
            for live, result in interrupted:
                await self._record_command_interrupt(live, result)
            if len(errors) == 1:
                raise errors[0]
            if errors:
                raise BaseExceptionGroup("打断命令失败", list(errors))
        self.changed.set()

    async def _record_command_interrupt(
        self,
        live: LiveCommand,
        result: object,
    ) -> None:
        events = await self._runtime.snapshot(self._session_id)
        dispatch = next(
            event
            for event in events
            if isinstance(event.payload, DispatchAttemptStarted)
            and event.payload.attempt_id == live.attempt_id
        )
        await self._runtime.dispatcher.accept_outcome(
            self._session_id,
            live.command_id,
            live.attempt_id,
            dispatch.event_id,
            CommandOutcome(OutcomeStatus.SUCCEEDED, value=result),
        )

    async def close(self) -> None:
        await self._runtime.dispatcher.close()
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def pending_authorization_ids(state: CanonicalState) -> tuple[str, ...]:
    return tuple(
        item.split(":", 1)[1]
        for item in state.waiting_for
        if item.startswith("authorization:")
    )
