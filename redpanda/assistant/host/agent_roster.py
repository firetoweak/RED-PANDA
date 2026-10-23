"""Host-owned roster rebuilt from Session facts, without a second identity store."""
import asyncio
from dataclasses import replace

from redpanda.assistant.agents import AgentRecord, project_agent
from redpanda.runtime import SqliteJournal


class AgentNameTaken(ValueError):
    pass


class AgentNotFound(LookupError):
    pass


class AgentDeletedError(LookupError):
    pass


class AgentRoster:
    """An in-memory index over Agent identity and project facts.

    Built from the Journals on first use, then kept current as the Host creates
    Agents and changes their names or projects; a restart rebuilds it from the facts.
    """

    def __init__(self, store):
        self.store = store
        self._entries: dict[str, AgentRecord] | None = None
        self._loading = asyncio.Lock()

    async def _index(self) -> dict[str, AgentRecord]:
        async with self._loading:
            if self._entries is None:
                entries = {}
                names = set()
                for path in self.store.journals():
                    journal = SqliteJournal(path)
                    session_id = await journal.session_identity()
                    events = await journal.snapshot(session_id)
                    agent = project_agent(events)
                    if agent is None:
                        continue
                    if names.intersection(agent.names):
                        raise ValueError("duplicate Agent name in persisted roster")
                    names.update(agent.names)
                    entries[session_id] = agent
                self._entries = entries
            return self._entries

    async def add(self, agent: AgentRecord) -> None:
        """Called once the creation fact is durable."""
        (await self._index())[agent.session_id] = agent

    async def refresh(self, session_id: str) -> AgentRecord:
        events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
        agent = project_agent(events)
        if agent is None:
            raise AgentNotFound(session_id)
        (await self._index())[session_id] = agent
        return agent

    async def resolve_recorded(self, name: str) -> AgentRecord:
        """Resolve names in existing facts; new addresses use resolve instead."""
        for entry in await self.list(include_deleted=True):
            if name in entry.names:
                return entry
        raise AgentNotFound(name)

    async def set_projects(self, session_id: str, projects) -> None:
        """Called once the Bot's own link or unlink fact is durable."""
        index = await self._index()
        entry = index[session_id]
        index[session_id] = replace(entry, projects=tuple(projects))

    async def list(self, *, include_deleted=False) -> tuple[AgentRecord, ...]:
        return tuple(sorted((entry for entry in (await self._index()).values()
                             if include_deleted or not entry.deleted), key=lambda entry: entry.name))

    async def resolve(self, name: str) -> AgentRecord:
        for entry in await self.list():
            if entry.name == name:
                return entry
        raise AgentNotFound(name)

    async def by_session(self, session_id: str) -> AgentRecord | None:
        return (await self._index()).get(session_id)
