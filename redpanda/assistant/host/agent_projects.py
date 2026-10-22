"""Mounting registered projects into Bots: one worktree and branch per Bot and project."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
import re

from redpanda.assistant.agents import AgentRecord, ProjectLinked, ProjectUnlinked, fact_arguments, project_agent
from redpanda.assistant.host.agent_worktrees import AgentProjectRequired, GitWorktree
from redpanda.filesystem.registry import WorkspaceRecord, WorkspaceRegistryError
from redpanda.runtime import DomainFactCommitted, SqliteJournal

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ProjectLinkRefused(ValueError):
    """A known reason the project cannot be linked or unlinked; nothing changed."""

    def __init__(self, code: str, error: str, *, hint: str | None = None, data: dict | None = None):
        super().__init__(error)
        self.code, self.error, self.hint, self.data = code, error, hint, data

    def result(self) -> dict:
        return {"ok": False, "code": self.code, "error": self.error,
                **({} if self.hint is None else {"hint": self.hint}),
                **({} if self.data is None else {"data": self.data})}


def _directory_name(name: str) -> str:
    cleaned = _UNSAFE.sub("-", name).strip(" .")
    return cleaned or "project"


class AgentProjects:
    def __init__(self, host):
        self.host = host
        self._locks: dict[str, asyncio.Lock] = {}
        self._landing: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def _agent(self, session_id: str) -> AgentRecord:
        agent = await self.host.agents.by_session(session_id)
        if agent is None:
            raise ValueError("project linking requires a Bot identity")
        return agent

    def resolve(self, *, workspace_id: str | None = None, name: str | None = None,
                path: str | None = None) -> WorkspaceRecord:
        """Find, or for a local path register in place, the project to link."""
        registry = self.host.workspaces
        if workspace_id is not None:
            return registry.get(workspace_id)
        if name is not None:
            matches = [record for record in registry.workspaces if record.name == name]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise ProjectLinkRefused(
                    "PROJECT_NOT_FOUND", "没有叫这个名字的项目。",
                    hint="从 data.projects 里选一个名字；要挂的项目还没登记时，用 path 给出它的本地路径。",
                    data={"projects": sorted({record.name for record in registry.workspaces})})
            raise ProjectLinkRefused(
                "PROJECT_AMBIGUOUS", "有多个项目叫这个名字。",
                hint="用 path 指明要挂的那一个。",
                data={"paths": [str(record.task_root) for record in matches]})
        root = Path(path).expanduser()
        if not root.is_absolute():
            raise ProjectLinkRefused("INVALID_PATH", "path 必须是绝对路径。")
        root = root.resolve()
        existing = next((record for record in registry.workspaces if record.task_root == root), None)
        if existing is not None:
            return existing
        try:
            self.host.agent_worktrees.require_project(root)
            return registry.register_path(root)
        except AgentProjectRequired as error:
            raise ProjectLinkRefused("PROJECT_UNUSABLE", str(error)) from None
        except (OSError, WorkspaceRegistryError) as error:
            raise ProjectLinkRefused("PROJECT_UNUSABLE", f"无法登记这个目录：{error}") from None

    def prepare(self, agent_directory: Path, session_id: str, project: WorkspaceRecord,
                taken: tuple[ProjectLinked, ...]) -> ProjectLinked:
        """Create the Bot's worktree for the project; the caller commits the fact."""
        names = {item.name for item in taken}
        base = _directory_name(project.name)
        name, index = base, 2
        while name in names or (agent_directory / "projects" / name).exists():
            name, index = f"{base}-{index}", index + 1
        path = agent_directory / "projects" / name
        try:
            worktree = self.host.agent_worktrees.link(project, session_id, path)
        except AgentProjectRequired as error:
            raise ProjectLinkRefused("PROJECT_UNUSABLE", str(error)) from None
        return ProjectLinked(workspace_id=project.workspace_id, name=name,
                             worktree=str(worktree.path), branch=worktree.branch)

    async def link(self, session_id: str, project: WorkspaceRecord, delivery_id: str) -> ProjectLinked:
        async with self._lock(session_id):
            agent = await self._agent(session_id)
            current = agent.project_by_id(project.workspace_id)
            if current is not None:
                raise ProjectLinkRefused(
                    "ALREADY_LINKED", "这个项目已经挂在这里。",
                    data={"project": current.name, "directory": current.worktree})
            linked = await asyncio.to_thread(self.prepare, agent.directory, session_id, project, agent.projects)
            arguments = fact_arguments(linked, delivery_id=delivery_id)
            try:
                await self.host.request("fact", session_id, arguments)
            except BaseException as error:
                try:
                    # A failed acknowledgement does not undo the recipient's acceptance.
                    events = await SqliteJournal(self.host.store.require(session_id)).snapshot(session_id)
                    accepted = next((event for event in events if event.delivery is not None
                                     and event.delivery.source == arguments["source"]
                                     and event.delivery.delivery_id == delivery_id), None)
                    if accepted is None:
                        await asyncio.to_thread(self.host.agent_worktrees.remove, project,
                                                GitWorktree(Path(linked.worktree), linked.branch))
                    else:
                        if accepted.payload != DomainFactCommitted(arguments["fact_type"], arguments["data"]):
                            raise ValueError("project link result conflicts with its request")
                        await self.host.agents.set_projects(session_id, project_agent(events).projects)
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup("project link and reconciliation failed",
                                             [error, cleanup_error]) from None
                raise
            await self.host.agents.set_projects(session_id, (*agent.projects, linked))
            return linked

    async def unlink(self, session_id: str, name: str, delivery_id: str) -> ProjectLinked:
        async with self._lock(session_id):
            agent = await self._agent(session_id)
            linked = self._require(agent, name)
            if await self.host.has_active_children(session_id, linked.workspace_id):
                raise ProjectLinkRefused(
                    "CHILDREN_ACTIVE", "还有委派到这个项目的子 Agent 没有交回。",
                    hint="等它们交回并处理完结果，或用 reclaim 收回，再解除。")
            project = self.host.workspaces.get(linked.workspace_id)
            async with self._landing.setdefault(linked.workspace_id, asyncio.Lock()):
                blocked = await asyncio.to_thread(self.host.agent_worktrees.unlink, project, linked)
            if blocked is not None:
                raise ProjectLinkRefused(
                    "UNLINK_BLOCKED", "解除会丢掉这个项目里还没落地的工作，没有解除。",
                    hint="把要保留的改动提交并用 land_branch 落地；不需要的改动丢弃与否由你决定，处理后再解除。",
                    data={"uncommitted": list(blocked.uncommitted), "unlanded_commits": blocked.unlanded,
                          "merge_in_progress": blocked.merging,
                          **({} if blocked.git is None else {"git": blocked.git})})
            await self.host.request("fact", session_id, fact_arguments(
                ProjectUnlinked(workspace_id=linked.workspace_id, name=linked.name), delivery_id=delivery_id))
            await self.host.agents.set_projects(
                session_id, tuple(item for item in agent.projects if item.workspace_id != linked.workspace_id))
            return linked

    def _require(self, agent: AgentRecord, name: str) -> ProjectLinked:
        linked = agent.project(name)
        if linked is None:
            raise ProjectLinkRefused(
                "PROJECT_NOT_LINKED", "你这里没有挂这个项目。",
                hint="用 data.projects 里的名字；要用的项目还没挂上时，先用 link_project 挂上。",
                data={"projects": [item.name for item in agent.projects]})
        return linked

    async def land(self, session_id: str, name: str) -> dict:
        agent = await self._agent(session_id)
        linked = self._require(agent, name)
        async with self._landing.setdefault(linked.workspace_id, asyncio.Lock()):
            return asdict(await asyncio.to_thread(self.host.agent_worktrees.land, agent, linked))

    async def sync(self, session_id: str, name: str) -> dict:
        agent = await self._agent(session_id)
        linked = self._require(agent, name)
        return asdict(await asyncio.to_thread(self.host.agent_worktrees.sync, agent, linked))
