"""Human-owned Bot deletion; histories and working files remain available."""
from __future__ import annotations

import asyncio

from redpanda.assistant.agents import AgentDeleted, fact_arguments
from redpanda.assistant.host.agent_roster import AgentNotFound
from redpanda.assistant.subagent.subagent import project_delegate_intents
from redpanda.runtime import AgentRuntime, SqliteJournal


class AgentLifecycle:
    def __init__(self, host):
        self.host = host
        self.retired: set[str] = set()

    async def is_deleted(self, session_id: str) -> bool:
        if session_id in self.retired:
            return True
        agent = await self.host.agents.by_session(session_id)
        if agent is not None:
            return agent.deleted
        reader = self.host.compact.store.reader_job(session_id)
        return reader is not None and await self.is_deleted(reader["source"])

    async def _stop(self, session_id: str) -> None:
        self.retired.add(session_id)
        self.host.intentionally_stopped.add(session_id)
        async with self.host.locks.setdefault(session_id, asyncio.Lock()):
            worker = self.host.workers.get(session_id)
            if worker is not None:
                worker.intentionally_stopped = True
                if not worker.exited.is_set() and worker.process.is_alive():
                    worker.process.terminate()
            if worker is not None:
                await worker.exited.wait()
            # IPC handlers have settled, including any command spawn already in progress.
            if self.host.commands is not None:
                await self.host.commands.manager.stop_session(session_id)
        await self.host.compact.retire(session_id)

    async def _retire_tree(self, session_id: str) -> None:
        await self._stop(session_id)
        events = await SqliteJournal(self.host.store.require(session_id)).snapshot(session_id)
        for intent in project_delegate_intents(events):
            self.retired.add(intent.child_session_id)
            if self.host.store.path(intent.child_session_id).parent.exists():
                await self._retire_tree(intent.child_session_id)
        self.host.automation.schedules.cancel_session(session_id)
        self.host.automation.changed.set()
        self.host.routines.store.delete_session(session_id)
        self.host.routines.changed.set()

    async def delete(self, session_id: str) -> None:
        agent = await self.host.agents.by_session(session_id)
        if agent is None:
            raise AgentNotFound(session_id)
        if not agent.deleted:
            await self._stop(session_id)
            try:
                await AgentRuntime(SqliteJournal(self.host.store.require(session_id)), None, {}).receive_domain_fact(
                    session_id, **fact_arguments(AgentDeleted(session_id=session_id, name=agent.name), delivery_id="deleted"))
            finally:
                recorded = await self.host.agents.refresh(session_id)
                if not recorded.deleted:
                    self.retired.remove(session_id)
        await self._retire_tree(session_id)
        await self.notify(session_id)

    async def recover(self) -> None:
        for entry in await self.host.agents.list(include_deleted=True):
            if entry.deleted:
                await self._retire_tree(entry.session_id)

    async def notify(self, session_id: str) -> None:
        agent = await self.host.agents.by_session(session_id)
        await self.host.agent_messages._broadcast(agent, AgentDeleted(session_id=session_id, name=agent.name),
                                                f"deleted:{session_id}")
        for group in await self.host.agent_groups.list():
            if any(member in agent.names for member in group.members):
                self.host.agent_groups._changed(group.group_id)
        await self.host._emit_session_activity(session_id, "idle")
