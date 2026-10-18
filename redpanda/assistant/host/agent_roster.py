"""Host-owned roster rebuilt from Session facts, without a second identity store."""
import asyncio
from dataclasses import dataclass, replace

from redpanda.assistant.agents import AgentRecord, project_agent, project_direction
from redpanda.runtime import SqliteJournal


class AgentNameTaken(ValueError):
    pass


class AgentNotFound(LookupError):
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
                    key = (agent.workspace_id, agent.name)
                    if key in names:
                        raise ValueError("duplicate Agent name in persisted project roster")
                    names.add(key)
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

    async def list(self, workspace_id: str | None = None) -> tuple[AgentView, ...]:
        entries = [entry for entry in (await self._index()).values()
                   if workspace_id is None or entry.agent.workspace_id == workspace_id]
        return tuple(sorted(entries, key=lambda entry: (entry.agent.workspace_id, entry.agent.name)))

    async def resolve(self, workspace_id: str, name: str) -> AgentRecord:
        for entry in await self.list(workspace_id):
            if entry.agent.name == name:
                return entry.agent
        raise AgentNotFound(name)

    async def by_session(self, session_id: str) -> AgentRecord | None:
        entry = (await self._index()).get(session_id)
        return None if entry is None else entry.agent
