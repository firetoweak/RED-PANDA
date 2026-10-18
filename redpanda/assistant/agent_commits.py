"""Each settled Step of a Coding Bot Agent becomes a commit on its own branch."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from redpanda.assistant.agent_git import Git
from redpanda.assistant.agents import AgentRecord
from redpanda.runtime import DomainFactCommitted
from redpanda.runtime.model import CommandPhase


STEP_COMMIT_FACT = "coding_bot.step_commit"


@dataclass(frozen=True, slots=True)
class StepCommit:
    step_id: str
    commit: str

    @classmethod
    def parse(cls, data) -> StepCommit:
        if set(data) != {"step_id", "commit"}:
            raise ValueError("step commit fact fields invalid")
        if any(type(data[key]) is not str or not data[key] for key in data):
            raise ValueError("step commit fact values invalid")
        return cls(data["step_id"], data["commit"])


def project_step_commits(events) -> tuple[StepCommit, ...]:
    return tuple(
        StepCommit.parse(event.payload.data) for event in events
        if isinstance(event.payload, DomainFactCommitted) and event.payload.fact_type == STEP_COMMIT_FACT
    )


class AgentStepCommits:
    """Commits the worktree only when no command of any Step is still running.

    The whole worktree is one commit, so a partly executed Step must not be
    split across two of them. The fact maps the Step to the branch head it left.
    """

    def __init__(self, runtime, session_id: str, agent: AgentRecord, git: Git) -> None:
        self.runtime = runtime
        self.session_id = session_id
        self.agent = agent
        self.git = git

    async def sync(self) -> None:
        events = await self.runtime.snapshot(self.session_id)
        state = self.runtime.projector.project_visible(self.session_id, events)
        if not state.steps or not all(
            command.phase is CommandPhase.TERMINAL or command.authorization_rejected_by_event_id is not None
            for step in state.steps for command in step.commands
        ):
            return
        last = state.steps[-1]
        if any(fact.step_id == last.step.step_id for fact in project_step_commits(events)):
            return
        commit = await asyncio.to_thread(self.git.commit_all, self.agent.worktree, self.agent.name)
        await self.runtime.receive_domain_fact(
            self.session_id, STEP_COMMIT_FACT, {"step_id": last.step.step_id, "commit": commit},
            delivery_id=last.step.step_id, source=STEP_COMMIT_FACT,
            causation_id=last.committed_event_id,
        )
