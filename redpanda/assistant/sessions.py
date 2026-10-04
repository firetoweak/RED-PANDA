from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from redpanda.assistant.control import (
    CONTROL_SOURCE,
    AssistantControlPlane,
    ControlApprovalView,
    ControlDecisionConflict,
    ControlOutcome,
    NoPendingControlApproval,
    pending_approval_view,
    project_control,
    project_control_message,
)
from redpanda.assistant.runner import (
    SessionScheduler,
    pending_authorization_ids,
    resume_session,
)
from redpanda.assistant.toolsets import ToolSurface
from redpanda.assistant.management import ManagementSurface
from redpanda.assistant.catalog import CapabilityCatalog
from redpanda.assistant.subagent.subagent import SubAgentHost, project_pending
from redpanda.runtime import AgentRuntime, RuntimeStatus
from redpanda.runtime.model import CanonicalState, CommandPhase


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    """待授权命令的展示信息：用户确认前需要看到自己在批准什么。"""

    command_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SessionView:
    status: str
    waiting_for: tuple[str, ...]
    pending_authorization_ids: tuple[str, ...]
    should_wake: bool
    has_active_subagents: bool = False
    control_approval: ControlApprovalView | None = None
    control_message: str | None = None
    auto_authorize: bool = False
    paused: bool = False
    pending_authorization_commands: tuple[PendingAuthorization, ...] = ()


def session_view(
    state: CanonicalState,
    *,
    control_approval: ControlApprovalView | None = None,
    control_message: str | None = None,
    has_active_subagents: bool = False,
    auto_authorize: bool = False,
    paused: bool = False,
) -> SessionView:
    pending_ids = pending_authorization_ids(state)
    by_id = {
        command_state.command.command_id: command_state.command
        for command_state in state.commands
    }
    return SessionView(
        status=state.status.value,
        waiting_for=state.waiting_for,
        pending_authorization_ids=pending_ids,
        should_wake=(
            state.status is RuntimeStatus.RUNNABLE
            or any(
                command.phase is CommandPhase.PENDING for command in state.commands
            )
        ),
        has_active_subagents=has_active_subagents,
        control_approval=control_approval,
        control_message=control_message,
        auto_authorize=auto_authorize,
        paused=paused,
        pending_authorization_commands=tuple(
            PendingAuthorization(
                command_id=command_id,
                name=by_id[command_id].effect.name,
                arguments=by_id[command_id].effect.argument_dict(),
            )
            for command_id in pending_ids
            if command_id in by_id
        ),
    )


class AssistantSessions:
    """给定 Session identity 后的 Assistant 应用操作。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        surface: ToolSurface,
        scheduler: SessionScheduler,
        *,
        control: AssistantControlPlane,
        management: ManagementSurface,
        catalog: CapabilityCatalog,
        subagents: SubAgentHost | None = None,
        workspace_versions=None,
    ) -> None:
        self._runtime = runtime
        self._surface = surface
        self._scheduler = scheduler
        self._control = control
        self._management = management
        self._catalog = catalog
        self._subagents = subagents
        self._workspace_versions = workspace_versions
        self._auto_authorize: dict[str, bool] = {}
        self._control_locks: dict[str, asyncio.Lock] = {}

    def _view(
        self,
        state: CanonicalState,
        events,
        *,
        has_active_subagents: bool = False,
    ) -> SessionView:
        return session_view(
            state,
            control_approval=pending_approval_view(events),
            control_message=project_control_message(events),
            has_active_subagents=has_active_subagents,
            auto_authorize=self._auto_authorize.get(state.session_id, False),
        )

    async def create(self, session_id: str) -> SessionView:
        created = await self._runtime.create_session(session_id)
        if not created:
            raise ValueError(f"Session 已存在: {session_id}")
        return await self.view(session_id)

    async def resume(self, session_id: str) -> SessionView:
        await resume_session(
            self._runtime,
            self._surface,
            session_id,
            self._management,
            self._catalog,
        )
        await self.recover_control(session_id)
        pending_subagents: tuple[str, ...] = ()
        if self._subagents is not None:
            pending_subagents = await self._subagents.rehydrate(session_id)
        events = await self._runtime.snapshot(session_id)
        state = await self._runtime.state(session_id)
        if self._subagents is not None and self._subagents.has_returned(session_id):
            return self._view(state, events)
        if self._view(state, events).should_wake:
            await self._scheduler.wake(session_id)
        elif self._subagents is not None:
            await self._subagents.on_quiesced(session_id, state)
        return self._view(
            state,
            events,
            has_active_subagents=bool(pending_subagents),
        )

    async def view(self, session_id: str) -> SessionView:
        events = await self._runtime.snapshot(session_id)
        state = await self._runtime.state(session_id)
        has_active_subagents = False
        if self._subagents is not None:
            has_active_subagents = bool(project_pending(events))
        return self._view(
            state,
            events,
            has_active_subagents=has_active_subagents,
        )

    async def resolve_control(
        self,
        session_id: str,
        request_id: str,
        *,
        approved: bool,
    ) -> str:
        async with self._control_locks.setdefault(session_id, asyncio.Lock()):
            events = await self._runtime.snapshot(session_id)
            projection = project_control(events)
            state = projection.get(request_id)
            if state is None:
                raise ControlDecisionConflict(f"未知控制请求: {request_id}")
            if state.phase == "rejected":
                if approved:
                    raise ControlDecisionConflict("控制请求已经被拒绝")
                assert state.message is not None
                return state.message
            if state.phase in {"succeeded", "failed"}:
                if not approved:
                    raise ControlDecisionConflict("控制请求已经被批准并执行")
                assert state.message is not None
                return state.message
            if state.phase == "execution_started":
                if not approved:
                    raise ControlDecisionConflict("控制请求已经开始执行")
                assert projection.message is not None
                return projection.message
            if state.phase == "approved":
                if not approved:
                    raise ControlDecisionConflict("控制请求已经被批准")
                return await self._execute_approved(session_id)
            if state.phase != "proposed" or projection.active is not state:
                raise NoPendingControlApproval(session_id)
            assert state.request is not None
            decision = self._control.decision_outcome(
                state.request,
                approved=approved,
            )
            await self._commit_control(session_id, decision)
            if not approved:
                await self._scheduler.wake(session_id)
                return decision.data["message"]
            return await self._execute_approved(session_id)

    async def _commit_control(
        self,
        session_id: str,
        outcome: ControlOutcome,
    ) -> None:
        await self._runtime.receive_domain_fact(
            session_id,
            outcome.fact_type,
            dict(outcome.data),
            delivery_id=outcome.delivery_id,
            source=CONTROL_SOURCE,
            requests_decision=outcome.requests_decision,
        )

    async def _execute_approved(self, session_id: str) -> str:
        projection = project_control(await self._runtime.snapshot(session_id))
        state = projection.active
        if state is None or state.phase != "approved" or state.request is None:
            raise RuntimeError("控制执行没有已批准请求")
        request = state.request
        await self._commit_control(
            session_id,
            self._control.execution_started_outcome(request),
        )
        execution = await self._control.execute(request)
        terminal = self._control.terminal_outcome(request, execution)
        await self._commit_control(session_id, terminal)
        await self._scheduler.wake(session_id)
        return execution.message

    async def recover_control(self, session_id: str) -> None:
        async with self._control_locks.setdefault(session_id, asyncio.Lock()):
            outcome = await self._control.prepare_pending(
                await self._runtime.snapshot(session_id)
            )
            if outcome is not None:
                await self._commit_control(session_id, outcome)
                if outcome.requests_decision:
                    await self._scheduler.wake(session_id)
            active = project_control(
                await self._runtime.snapshot(session_id)
            ).active
            if active is not None and active.phase == "approved":
                await self._execute_approved(session_id)

    async def receive_user_message(
        self,
        session_id: str,
        content: str,
        *,
        delivery_id: str,
        source: str = "user",
        artifact_refs: tuple[str, ...] = (),
    ) -> None:
        await self._runtime.receive_user_message(
            session_id,
            content,
            delivery_id=delivery_id,
            source=source,
            artifact_refs=tuple(artifact_refs),
        )
        await self._scheduler.wake(session_id)

    async def accept_input(
        self,
        session_id: str,
        content: str,
        *,
        delivery_id: str,
        source: str = "user",
        artifact_refs: tuple[str, ...] = (),
    ) -> SessionView:
        view = await self.view(session_id)
        answer = content.strip().lower()
        if view.control_approval is not None and answer in {"yes", "y", "no", "n"}:
            await self.resolve_control(
                session_id,
                view.control_approval.request_id,
                approved=answer in {"yes", "y"},
            )
            return await self.view(session_id)
        if view.pending_authorization_ids and answer in {"yes", "y", "no", "n"}:
            await self.resolve_authorizations(
                session_id,
                approved=answer in {"yes", "y"},
            )
            return await self.view(session_id)
        await self.receive_user_message(
            session_id,
            content,
            delivery_id=delivery_id,
            source=source,
            artifact_refs=tuple(artifact_refs),
        )
        return await self.view(session_id)

    async def resolve_authorization(
        self,
        session_id: str,
        command_id: str,
        *,
        approved: bool,
    ) -> None:
        if approved:
            await self._runtime.grant_command(session_id, command_id)
        else:
            await self._runtime.reject_command(session_id, command_id)
        await self._scheduler.wake(session_id)

    async def resolve_authorizations(
        self,
        session_id: str,
        *,
        approved: bool,
    ) -> None:
        state = await self._runtime.state(session_id)
        for command_id in pending_authorization_ids(state):
            if approved:
                await self._runtime.grant_command(session_id, command_id)
            else:
                await self._runtime.reject_command(session_id, command_id)
        await self._scheduler.wake(session_id)

    def is_auto_authorized(self, session_id: str) -> bool:
        return self._auto_authorize.get(session_id, False)

    async def apply_auto_authorize(
        self,
        session_id: str,
        *,
        enabled: bool,
    ) -> SessionView:
        self._auto_authorize[session_id] = bool(enabled)
        if enabled:
            await self._grant_pending(session_id)
        return await self.view(session_id)

    async def _grant_pending(self, session_id: str) -> None:
        state = await self._runtime.state(session_id)
        pending = pending_authorization_ids(state)
        if not pending:
            return
        for command_id in pending:
            await self._runtime.grant_command(session_id, command_id)
        await self._scheduler.wake(session_id)

    async def cancel_turn(self, session_id: str) -> SessionView:
        await self._scheduler.cancel_turn(session_id)
        return await self.view(session_id)

    async def settle_forked_workspace(
        self, session_id: str, restore: bool, delivery_id: str
    ) -> None:
        assert self._workspace_versions is not None
        await self._workspace_versions.settle_fork(restore, delivery_id)
