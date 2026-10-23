"""Host-owned roster rebuilt from Session facts, without a second identity store."""
import asyncio
from dataclasses import dataclass, replace

from redpanda.assistant.agents import AgentRecord, project_agent, project_direction
from redpanda.runtime import SqliteJournal


class AgentNameTaken(ValueError):
    pass


class AgentNotFound(LookupError):
    pass


class AgentDeletedError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class AgentView:
    agent: AgentRecord
    direction: str | None


class AgentRoster:
    """An in-memory index over Agent creation and direction facts.

    Built from the Journals on first use, then kept current as the Host creates
    Agents and relays their declarations; a restart rebuilds it from the facts.
    """

    def __init__(self, store):
        self.store = store
        self._entries: dict[str, AgentView] | None = None
        self._loading = asyncio.Lock()

    async def _index(self) -> dict[str, AgentView]:
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
                    entries[session_id] = AgentView(agent, project_direction(events, agent))
                self._entries = entries
            return self._entries

    async def add(self, agent: AgentRecord) -> None:
        """Called once the creation fact is durable."""
        (await self._index())[agent.session_id] = AgentView(agent, None)

    async def declare(self, session_id: str, direction: str) -> None:
        """Called once the Agent's own declaration fact is durable."""
        index = await self._index()
        index[session_id] = replace(index[session_id], direction=direction)

    async def refresh(self, session_id: str) -> AgentRecord:
        events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
        agent = project_agent(events)
        if agent is None:
            raise AgentNotFound(session_id)
        (await self._index())[session_id] = AgentView(agent, project_direction(events, agent))
        return agent

    async def resolve_recorded(self, name: str) -> AgentRecord:
        """Resolve names in existing facts; new addresses use resolve instead."""
        for entry in await self.list(include_deleted=True):
            if name in entry.agent.names:
                return entry.agent
        raise AgentNotFound(name)

    async def set_projects(self, session_id: str, projects) -> None:
        """Called once the Bot's own link or unlink fact is durable."""
        index = await self._index()
        entry = index[session_id]
        index[session_id] = replace(entry, agent=replace(entry.agent, projects=tuple(projects)))

    async def list(self, *, include_deleted=False) -> tuple[AgentView, ...]:
        return tuple(sorted((entry for entry in (await self._index()).values()
                             if include_deleted or not entry.agent.deleted), key=lambda entry: entry.agent.name))

    async def resolve(self, name: str) -> AgentRecord:
        for entry in await self.list():
            if entry.agent.name == name:
                return entry.agent
        raise AgentNotFound(name)

    async def by_session(self, session_id: str) -> AgentRecord | None:
        entry = (await self._index()).get(session_id)
        return None if entry is None else entry.agent
