from __future__ import annotations

import asyncio
from dataclasses import dataclass

from redpanda.assistant.control import AssistantControlPlane
from redpanda.assistant.runner import SessionScheduler
from redpanda.runtime import AgentRuntime
from redpanda.runtime.model import CanonicalState


class SettlingScheduler(SessionScheduler):
    def __init__(self, *args, control=None, **kwargs) -> None:
        super().__init__(
            *args,
            control=(
                AssistantControlPlane(())
                if control is None
                else control
            ),
            **kwargs,
        )

    async def join(self) -> None:
        while True:
            await asyncio.sleep(0)
            if self._failure is not None:
                raise self._failure
            tasks = (
                *((self._task,) if self._task is not None else ()),
                *self._runtime.dispatcher._tasks.values(),
            )
            if not tasks:
                await asyncio.sleep(0)
                if self._task is None and not self._runtime.dispatcher._tasks:
                    return
                continue
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)


class RecordingScheduler(SettlingScheduler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.woken: list[str] = []

    async def wake(self, session_id: str) -> None:
        self.woken.append(session_id)
        await super().wake(session_id)


@dataclass(frozen=True, slots=True)
class SettledSession:
    state: CanonicalState
    control_message: str | None = None


async def build_settling_assistant(config, sink, journal, session_id, workspace):
    from redpanda.assistant.assembly import build_assistant_assembly

    return await build_assistant_assembly(
        config,
        sink,
        journal,
        session_id=session_id,
        workspace=workspace,
        scheduler_factory=SettlingScheduler,
    )


async def settle_session(
    runtime: AgentRuntime,
    session_id: str,
    *,
    control: AssistantControlPlane | None = None,
) -> SettledSession:
    scheduler = SettlingScheduler(
        runtime,
        session_id,
        control=control,
    )
    try:
        await scheduler.wake(session_id)
        await scheduler.join()
        return SettledSession(await runtime.state(session_id))
    finally:
        await scheduler.close()
