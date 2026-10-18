"""Host-owned roster rebuilt from Session facts, without a second identity store."""
from dataclasses import dataclass

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
    def __init__(self, store):
        self.store = store

    async def list(self, workspace_id: str | None = None) -> tuple[AgentView, ...]:
        entries = []
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
            if workspace_id is None or agent.workspace_id == workspace_id:
                entries.append(AgentView(agent, project_direction(events, agent)))
        return tuple(sorted(entries, key=lambda entry: (entry.agent.workspace_id, entry.agent.name)))

    async def resolve(self, workspace_id: str, name: str) -> AgentRecord:
        for entry in await self.list(workspace_id):
            if entry.agent.name == name:
                return entry.agent
        raise AgentNotFound(name)

    async def by_session(self, session_id: str) -> AgentRecord | None:
        journal = SqliteJournal(self.store.require(session_id))
        return project_agent(await journal.snapshot(session_id))
