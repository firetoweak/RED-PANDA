from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
from inspect import isawaitable
import multiprocessing
import os
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

from redpanda.automation.routine_tool import LIST_ROUTINES, SAVE_ROUTINE, SET_ROUTINE_STATE
from redpanda.automation.absence import (
    ABSENCE_REVIEW_SECONDS, AWAY_BEFORE_ASK, PAUSED_TEXT, RESUMED_TEXT, counts_since, next_absence_action,
)
from redpanda.automation.routines import ROUTINE_DUE, RoutineClock, RoutineFire, RoutineNotFound, RoutineStore
from redpanda.automation.once import (
    SOURCE as AUTOMATION_SOURCE,
    TIME_REACHED,
    OneShotClock,
    OneShotSchedules,
    ScheduleDeliveryUnavailable,
    ScheduledCheck,
)
from redpanda.assistant.control import pending_approval_view, project_control_message
from redpanda.assistant.session_metadata import SessionFlagStore
from redpanda.assistant.compact.host import CompactHost
from redpanda.assistant.attachments import AttachmentGateway, AttachmentRejected
from redpanda.assistant.audience import Audience
from redpanda.assistant.host.ipc import PipePeer, ProcessFailure, WorkerFailed
from redpanda.assistant.host.llm_port import complete_llm_chat
from redpanda.assistant.host.session_store import SessionStore
from redpanda.assistant.agents import (
    AGENT_CREATED, AgentCreated, AgentRecord, RoutinesUnwatched, fact_arguments, project_agent,
)
from redpanda.assistant.host.agent_roster import AgentNameTaken, AgentNotFound, AgentRoster
from redpanda.assistant.host.agent_worktrees import AgentWorktrees
from redpanda.assistant.agent_git import GitWorkspace
from redpanda.assistant.host.agent_messages import AgentMessages
from redpanda.assistant.host.agent_groups import AgentGroups
from redpanda.assistant.agent_messages import (
    DECLARE_DIRECTION, LIST_COLLEAGUES, MESSAGE_USER, SEND_GROUP_MESSAGE, SEND_MESSAGE,
)
from redpanda.assistant.sessions import session_view
from redpanda.assistant.subagent.subagent import (
    REPORT_FACT,
    RETURN_FACT,
    project_delegate_intents,
    persist_return,
    project_pending,
    project_parent,
    project_returned,
    project_task,
    report_arguments,
    return_data,
    task_from_arguments,
)
from redpanda.assistant.workspaces import UnboundSessionError, bound_workspace_id
from redpanda.assistant.host.spawn import start_worker
from redpanda.assistant.host.worker import worker_main
from redpanda.runtime import AgentRuntime, SqliteJournal, replay, DomainFactCommitted, AttemptOrphaned, CommandPhase
from redpanda.execution.local.windows_job import WindowsJob
from redpanda.execution.processes import ProcessManager
from redpanda.execution.command import CommandStartError, ShellNotFoundError
from redpanda.filesystem.registry import WorkspaceRegistry
from redpanda.config import write_json


@dataclass
class Worker:
    process: object
    peer: PipePeer
    reader: asyncio.Task | None = None
    requests: int = 0
    idle_revision: int | None = None
    stopping: bool = False
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    failure: WorkerFailed | None = None
    transition: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    idle_has_active_subagents: bool = False
    running: bool = False
    returned: tuple[str, dict] | None = None
    intentionally_stopped: bool = False
    active_preview: str | None = None
    active_thinking: str | None = None
    live_tools: dict[str, str] = field(default_factory=dict)
    # Who each output of this generation is for, read from the Journal once per output.


async def _emit(emitted) -> None:
    if isawaitable(emitted):
        await emitted


async def _settle(awaitable):
    """Wait for Git work even when the caller is cancelled.

    A cancelled request must not release Git ownership (a lock or a pending
    identity fact) while the work it started is still running.
    """
    work = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError as cancelled:
        try:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
            work.result()
        except BaseException as error:
            raise BaseExceptionGroup("Git work failed during cancellation", [cancelled, error]) from None
        raise


class HostSupervisor:
    """One local owner, one process per active Session. No Runtime projection."""

    def __init__(
        self,
        store: SessionStore,
        config_factory,
        home,
        sink,
        *,
        llm,
        command_environment=None,
        models=None,
        context_usage_sink=None,
        subagent_activity_sink=None,
        conversation_status_sink=None,
        tool_progress_sink=None,
        authorization_required_sink=None,
        preview_sink=None,
        thinking_sink=None,
        subagent_output_sink=None,
        session_activity_sink=None,
        session_failed_sink=None,
        schedule_changed_sink=None,
        group_changed_sink=None,
        workspaces=None,
    ):
        self.store = store
        self.config_factory = config_factory
        self.home = home
        self.sink = sink
        self.llm = llm
        # 启动来源的副本；不跟随产品 PATH 补全或后续 Worker 环境变化。
        self.command_environment = dict(
            os.environ if command_environment is None else command_environment
        )
        self.models = models
        self.context_usage_sink = context_usage_sink
        self.subagent_activity_sink = subagent_activity_sink
        self.conversation_status_sink = conversation_status_sink
        self.tool_progress_sink = tool_progress_sink
        self.authorization_required_sink = authorization_required_sink
        self.preview_sink = preview_sink
        self.thinking_sink = thinking_sink
        self.subagent_output_sink = subagent_output_sink
        self.session_activity_sink = session_activity_sink
        self.session_failed_sink = session_failed_sink
        self.schedule_changed_sink = schedule_changed_sink
        self.group_changed_sink = group_changed_sink
        self.workers: dict[str, Worker] = {}
        self.watchers: set[asyncio.Task] = set()
        self.locks: dict[str, asyncio.Lock] = {}
        self.failures: asyncio.Queue[WorkerFailed] = asyncio.Queue()
        self.intentionally_stopped: set[str] = set()
        self.selections: dict[str, str] = {}
        self.selecting: dict[str, str] = {}
        self.selection_locks: dict[str, asyncio.Lock] = {}
        self.turn_locks: dict[str, asyncio.Lock] = {}
        self._current_failures: dict[str, str] = {}
        self.closed = False
        self.compact = CompactHost(self)
        self.job = WindowsJob.create() if os.name == "nt" else None
        self.processes = (ProcessManager(home.state_root / "processes", self.command_environment)
                          if sys.platform in {"win32", "linux"} else None)
        self._auto_authorize = SessionFlagStore(store.root, "auto_authorize.json")
        self._pause = SessionFlagStore(store.root, "paused.json")
        self.automation = OneShotClock(
            OneShotSchedules(home.state_root / "automation.sqlite")
        )
        self._automation_task: asyncio.Task[None] | None = None
        self.routines = RoutineClock(RoutineStore(home.state_root / "routines.sqlite"))
        self._routine_task: asyncio.Task[None] | None = None
        self._absence_task: asyncio.Task[None] | None = None
        self.workspaces = (
            workspaces
            if workspaces is not None
            else WorkspaceRegistry.load(home.workspaces_path)
        )
        self.agents = AgentRoster(store)
        self.agent_messages = AgentMessages(self)
        self.agent_groups = AgentGroups(self)
        self.agent_worktrees = AgentWorktrees(home, self.command_environment)
        self._agent_creation_lock = asyncio.Lock()
        self._landing_locks: dict[str, asyncio.Lock] = {}

    async def resume_unfinished_sessions(self) -> None:
        """进程刚起来时，上一个进程里还会自己往下走的顶层会话接着推进。

        长期 Agent 通常自动续跑；人停止过或命令结果未知的会话等人继续。
        等外部事实的会话本来就不会自己动，子会话只由父恢复时带起来，这里都不改。

        此刻没有任何 Worker，上一个进程留下的未完成 Attempt 先按接管记下，
        否则判断看到的是一条永远等不到结果的命令。
        """

        unfinished = []
        joining = []
        agent_births = {}
        for path in self.store.journals():
            journal = SqliteJournal(path)
            session_id = await journal.session_identity()
            if not self.is_paused(session_id):
                await journal.prepare_recovery(session_id)
            events = await journal.snapshot(session_id)
            if project_parent(events) is not None:
                continue
            state = replay(session_id, events).state
            agent = project_agent(events)
            if agent is not None:
                born = next(event.occurred_at for event in events
                            if isinstance(event.payload, DomainFactCommitted)
                            and event.payload.fact_type == AGENT_CREATED)
                agent_births[session_id] = (agent, born)
            if self.is_paused(session_id):
                continue
            observed = max((step.step.observed_journal_position for step in state.steps), default=0)
            unknown = {command.command.command_id for command in state.commands
                       if command.phase is CommandPhase.UNKNOWN
                       and command.command.effect.name in {"execute_command", "start_process"}}
            if any(isinstance(event.payload, AttemptOrphaned)
                   and event.payload.command_id in unknown and event.sequence > observed for event in events):
                # An unknown execution may already have produced side effects.
                # Persist the stop before any Worker or external wake can advance it.
                self._pause.set(session_id, True)
                continue
            if agent is not None and ((path.parent / "agent-creation.json").exists() or not state.steps):
                joining.append(agent)
                continue
            if session_view(state).should_wake:
                unfinished.append(session_id)
        for agent in joining:
            pending = self.store.require(agent.session_id).parent / "agent-creation.json"
            if not pending.exists():
                # Repair Agents created before creation recovery was recorded explicitly.
                born = agent_births[agent.session_id][1]
                colleagues = [sid for sid, (other, time) in agent_births.items()
                              if other.workspace_id == agent.workspace_id and time < born]
                write_json(pending, {"colleagues": colleagues})
            await self._finish_agent_creation(agent)
        for session_id in unfinished:
            await self.resume(session_id)

    async def _parent_agent(self, session_id) -> AgentRecord | None:
        parent = project_parent(await SqliteJournal(self.store.require(session_id)).snapshot(session_id))
        return None if parent is None else await self.agents.by_session(parent)

    async def _git_workspace(self, session_id) -> GitWorkspace | None:
        """Agents and the tasks they delegate run directly in their own Git worktrees."""
        agent = await self.agents.by_session(session_id)
        if agent is not None:
            return GitWorkspace(agent.worktree, agent.name)
        parent = await self._parent_agent(session_id)
        if parent is None:
            return None
        return GitWorkspace(self.agent_worktrees.child(parent.workspace_id, session_id).path, parent.name)

    async def _copy_worktree_file(self, session_id: str, relative: str) -> dict:
        """Snapshot one worktree file into the session attachments the person can download."""
        if type(relative) is not str or not relative.strip() or Path(relative).is_absolute() or ".." in Path(relative).parts:
            return {"ok": False, "code": "INVALID_PATH", "error": "path 必须是工作区内的相对路径。"}
        workspace = await self._git_workspace(session_id)
        if workspace is None:
            return {"ok": False, "code": "FILE_REQUIRES_AGENT", "error": "只有 Agent 能把工作区文件交给人。"}
        root = workspace.worktree.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return {"ok": False, "code": "FILE_NOT_FOUND", "error": "工作区里没有这个文件。"}
        try:
            with target.open("rb") as stream:
                saved = AttachmentGateway(self.store.root).for_session(session_id).save_file_stream(stream, target.name)
        except (OSError, AttachmentRejected):
            return {"ok": False, "code": "FILE_UNREADABLE", "error": "这个文件无法读取，或名称不能作为附件。"}
        return {"attachment_id": saved.attachment_id, "name": saved.name, "size": saved.size}

    async def _route(self, operation, session_id, arguments, *, worker=None):
        if operation == "agent_child_review":
            parent = await self.agents.by_session(session_id)
            child = arguments["child_session_id"]
            if arguments["merge"]:
                result, changed = await _settle(asyncio.to_thread(self.agent_worktrees.merge_child, parent, child))
                return {**asdict(result), "changed_files": list(changed)}
            return await asyncio.to_thread(self.agent_worktrees.compare_child, parent, child,
                                           tuple(arguments["paths"]), arguments["offset"])
        if operation == LIST_COLLEAGUES:
            return await self.agent_messages.colleagues(session_id)
        if operation == SEND_MESSAGE:
            return await self.agent_messages.send(session_id, arguments)
        if operation == SEND_GROUP_MESSAGE:
            return await self.agent_messages.send_group(session_id, arguments)
        if operation == DECLARE_DIRECTION:
            return await self.agent_messages.declare(session_id, arguments)
        if operation == MESSAGE_USER:
            attachment = None
            if arguments.get("path") is not None:
                copied = await self._copy_worktree_file(session_id, arguments["path"])
                if copied.get("ok") is False:
                    return copied
                attachment = copied
            if self.sink is not None:
                await _emit(self.sink(session_id, arguments["output_id"], arguments["text"], "user"))
            return None if attachment is None else {"attachment": attachment}
        if operation in {"agent_land", "agent_sync"}:
            agent = await self.agents.by_session(session_id)
            if operation == "agent_sync":
                result = await _settle(asyncio.to_thread(self.agent_worktrees.sync, agent))
            else:
                async with self._landing_locks.setdefault(agent.workspace_id, asyncio.Lock()):
                    result = await _settle(asyncio.to_thread(self.agent_worktrees.land, agent))
            return asdict(result)
        if operation in {"process_start", "process_read", "process_stop", "process_list"}:
            self.store.require(session_id)
            if self.processes is None:
                raise RuntimeError("Persistent processes require Windows or Linux")
            if operation == "process_start":
                if self.closed:
                    raise RuntimeError("Cannot start process for a closed owner")
                try:
                    process = await self.processes.start(session_id, arguments["command"], Path(arguments["cwd"]))
                except ShellNotFoundError as exc:
                    return {"ok": False, "code": "SHELL_NOT_FOUND", "shell": exc.shell_name,
                            "executable": exc.executable}
                except CommandStartError as exc:
                    return {"ok": False, "code": "COMMAND_START_FAILED", "error": str(exc)}
                return {"ok": True, "process": process}
            if operation == "process_read":
                return await self.processes.read(session_id, arguments["process_id"],
                                                 arguments["stdout_offset"], arguments["stderr_offset"])
            if operation == "process_stop":
                return await self.processes.stop(session_id, arguments["process_id"])
            return await self.processes.list(session_id)
        if operation == "model_settings":
            if self.models is not None:
                return self.models.for_decision(session_id, apply=arguments["apply"])
            return None
        if operation == SAVE_ROUTINE:
            result = self.routines.store.save(
                arguments["command_id"], session_id, routine_id=arguments["routine_id"],
                name=arguments["name"], intent=arguments["intent"], schedule=arguments["schedule"])
            self.routines.changed.set()
            await self._emit_schedule_changed(session_id)
            return result
        if operation == SET_ROUTINE_STATE:
            result = self.routines.store.set_state(
                arguments["command_id"], session_id, arguments["routine_id"], arguments["state"])
            self.routines.changed.set()
            await self._emit_schedule_changed(session_id)
            return result
        if operation == LIST_ROUTINES:
            return {"ok": True, "code": "ROUTINES_READ", "data": {"routines": [
                routine.for_model() for routine in self.routines.store.list(session_id)]}}
        if operation == "cancel_schedule":
            code = self.automation.cancel(
                arguments["command_id"], session_id, arguments["schedule_id"],
            )
            if code == "CANCELLED":
                await self._emit_schedule_changed(session_id)
            return code
        if operation == "schedule_once":
            started_at = arguments.get("started_at")
            if started_at is not None:
                started_at = datetime.fromisoformat(started_at)
                if started_at.tzinfo is not timezone.utc:
                    raise ValueError("schedule started_at must use UTC")
            schedule = self.automation.register(
                arguments["schedule_id"],
                session_id,
                arguments["delay_seconds"],
                arguments["purpose"],
                started_at=started_at,
            )
            await self._emit_schedule_changed(session_id)
            return {
                "schedule_id": schedule.schedule_id,
                "due_at": schedule.due_at.astimezone().isoformat(),
                "purpose": schedule.purpose,
            }
        if operation == "llm_chat":
            if worker is None:
                worker = self.workers[session_id]

            async def on_delta(text):
                await worker.peer.send(("delta", worker.peer.active_request_id, text))

            async def on_reasoning_delta(text):
                await worker.peer.send(
                    ("reasoning_delta", worker.peer.active_request_id, text)
                )

            return await complete_llm_chat(
                self.llm, arguments, on_delta, on_reasoning_delta
            )
        if operation == "is_paused":
            return self.is_paused(session_id)
        if operation == "compact_boundary":
            return await self.compact.boundary(session_id, arguments)
        if operation == "compact_complete":
            return await self.compact.complete(session_id, arguments)
        if operation in {"output", "subagent_output"}:
            if self.compact.store.reader_job(session_id) is not None:
                return None
            sink = self.sink if operation == "output" else self.subagent_output_sink
            if sink is not None:
                audience = await self._audience(session_id)
                await _emit(sink(session_id, arguments["output_id"], arguments["text"], audience))
            if worker is not None and worker.active_preview == arguments["output_id"]:
                worker.active_preview = None
            return None
        if operation == "create_child":
            initial_fact = dict(arguments)
            expected_task = task_from_arguments(session_id, initial_fact)
            child_workspace_id = await self.bound_workspace_id(
                expected_task.parent_session_id
            )
            async with self.locks.setdefault(session_id, asyncio.Lock()):
                parent_workspace = self.workspaces.get(child_workspace_id)
                conflict_child_id = None
                if expected_task.resolve_conflicts_of is not None:
                    parent_events = await SqliteJournal(self.store.require(expected_task.parent_session_id)).snapshot(expected_task.parent_session_id)
                    source = next((item for item in project_delegate_intents(parent_events)
                                   if item.command_id == expected_task.resolve_conflicts_of), None)
                    if source is None:
                        raise ValueError("conflict source does not belong to parent")
                    source_files = await self._route("prepare_child_files", source.child_session_id,
                                                       {"parent_session_id": expected_task.parent_session_id})
                    if not source_files["ok"]:
                        raise ValueError("conflict source has no completed files")
                    conflict_child_id = source.child_session_id
                parent_agent = await self.agents.by_session(expected_task.parent_session_id)
                # A plain session's delegated task only reads the parent's own directory.
                if parent_agent is not None:
                    await _settle(asyncio.to_thread(self.agent_worktrees.create_child, parent_workspace,
                                                    parent_agent, session_id, conflict_child_id))
                if not self.store.path(session_id).parent.exists():
                    await self.store.create(
                        session_id,
                        workspace_id=child_workspace_id,
                        initial_fact=initial_fact,
                    )
                    if self.models is not None:
                        self.models.initialize_session(session_id, parent=expected_task.parent_session_id)
                else:
                    events = await SqliteJournal(
                        self.store.require(session_id)
                    ).snapshot(session_id)
                    if project_task(events) != expected_task:
                        raise ValueError(
                            "existing child Session does not match delegate intent"
                        )
                    if bound_workspace_id(events) != child_workspace_id:
                        raise ValueError(
                            "existing child Session workspace does not match parent"
                        )
            # delegate acknowledges durable creation, not successful initialization.
            activation = asyncio.create_task(
                self.request("fact", session_id, initial_fact)
            )
            self._track(session_id, activation)
            return None
        if operation == "prepare_child_files":
            events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
            task = project_task(events)
            if task is None or task.parent_session_id != arguments["parent_session_id"]:
                raise ValueError("child workspace owner does not match delegate")
            if not any(isinstance(event.payload, DomainFactCommitted)
                       and event.payload.fact_type == RETURN_FACT for event in events):
                return {"ok": False, "code": "CHILD_STILL_WORKING", "error": "子会话尚未交回或收回。"}
            parent_agent = await self.agents.by_session(task.parent_session_id)
            if parent_agent is not None:
                failure = await _settle(asyncio.to_thread(self.agent_worktrees.finish_child, parent_agent, session_id))
                if failure is not None:
                    return {"ok": False, "code": "CHILD_RESULT_FAILED", "data": {"git": failure},
                            "error": "整理这个子任务的成果时 Git 报错，暂时无法查看或合入它。",
                            "hint": "出错的位置不在你的工作区里，你无法处理。把 Git 原文告诉用户，请用户处理后再试。"}
            return {"ok": True}
        if operation == "reclaim_child":
            await self._reclaim_child(session_id, arguments)
            return None
        return await self.request(operation, session_id, arguments)

    def start_automation(self, group: asyncio.TaskGroup) -> None:
        self._automation_task = group.create_task(
            self.automation.run(self._deliver_scheduled, self._emit_schedule_changed),
            name="assistant-one-shot-clock",
        )
        self._routine_task = group.create_task(
            self.routines.run(self._deliver_routine, self._emit_schedule_changed),
            name="assistant-routine-clock",
        )
        self._absence_task = group.create_task(
            self._review_absence_loop(), name="assistant-absence-review",
        )

    async def _deliver_routine(self, fire: RoutineFire) -> None:
        try:
            await self.request("fact", fire.session_id, {
                "fact_type": ROUTINE_DUE,
                "data": {
                    "routine_id": fire.routine_id,
                    "name": fire.name,
                    "intent": fire.intent,
                    "planned_at": fire.planned_at.isoformat(),
                    "fired_at": fire.fired_at.isoformat(),
                },
                "delivery_id": fire.fire_id,
                "source": AUTOMATION_SOURCE,
                "requests_decision": True,
                "causation_id": fire.fire_id,
            })
        except WorkerFailed as error:
            raise ScheduleDeliveryUnavailable(fire.fire_id) from error

    def list_routines(self, session_id: str):
        return self.routines.store.list(session_id)

    async def set_routine_state_by_user(self, session_id: str, routine_id: str, state: str):
        """The app's pause, resume and cancel buttons; an Agent cannot undo a person's pause."""
        routine = next((item for item in self.routines.store.list(session_id) if item.routine_id == routine_id), None)
        if routine is None:
            raise RoutineNotFound(routine_id)
        updated = self.routines.store.set_state_by_user(routine_id, state)
        self.routines.changed.set()
        await self._emit_schedule_changed(session_id)
        return updated

    def note_web_connected(self) -> None:
        self.routines.store.note_connected(datetime.now(timezone.utc))

    def note_web_disconnected(self) -> None:
        self.routines.store.note_disconnected(datetime.now(timezone.utc))

    def absence_watches(self):
        return self.routines.store.watches()

    async def answer_absence(self, session_id: str, action: str) -> None:
        """The person's inbox choice. Continue restores only this pause's routines."""
        if await self.agents.by_session(session_id) is None:
            raise AgentNotFound(session_id)
        now = datetime.now(timezone.utc)
        if action == "stop":
            watch = self.routines.store.pause_for_absence(session_id)
            await self._tell_absence(session_id, "paused", watch.paused_ids, watch.asked_at)
            self.routines.store.mark_absence_told(session_id)
        elif action == "continue":
            restored = self.routines.store.resume_absence(session_id, now)
            if restored:
                await self._tell_absence(session_id, "resumed", restored, now)
        else:
            raise ValueError(action)
        self.routines.changed.set()
        await self._emit_schedule_changed(session_id)

    async def review_absence(self, now: datetime | None = None) -> None:
        """One pass: finish an untold pause, then ask or pause while the person is away."""
        now = now or datetime.now(timezone.utc)
        store = self.routines.store
        for watch in store.watches():
            if watch.phase == "paused" and not watch.fact_recorded:
                await self._tell_absence(watch.session_id, "paused", watch.paused_ids, watch.asked_at)
                store.mark_absence_told(watch.session_id)
                await self._emit_schedule_changed(watch.session_id)
        last_seen, connections = store.presence()
        if connections > 0 or (last_seen is not None and now - last_seen < AWAY_BEFORE_ASK):
            return
        for watch in store.watches():
            if next_absence_action(
                present=False, now=now, last_seen=last_seen, watch=watch, reports=0, triggers=0,
            ) == "pause":
                paused = store.pause_for_absence(watch.session_id)
                await self._tell_absence(watch.session_id, "paused", paused.paused_ids, paused.asked_at)
                store.mark_absence_told(watch.session_id)
                await self._emit_schedule_changed(watch.session_id)
        watched = {item.session_id for item in store.watches()}
        for entry in await self.agents.list():
            session_id = entry.agent.session_id
            if session_id in watched:
                continue
            events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
            reports, triggers = counts_since(session_id, events, last_seen)
            if next_absence_action(
                present=False, now=now, last_seen=last_seen, watch=None,
                reports=reports, triggers=triggers,
            ) != "ask":
                continue
            store.ask_absence(session_id, now)
            await self._emit_schedule_changed(session_id)

    async def _review_absence_loop(self) -> None:
        while True:
            await asyncio.sleep(ABSENCE_REVIEW_SECONDS)
            if self.closed:
                return
            await self.review_absence()

    async def _tell_absence(self, session_id: str, action: str, routine_ids, asked_at: datetime) -> None:
        """Record the result on the Agent's timeline without starting a turn."""
        text = PAUSED_TEXT if action == "paused" else RESUMED_TEXT
        identity = asked_at.isoformat()
        await AgentRuntime(SqliteJournal(self.store.require(session_id)), None, {}).receive_domain_fact(
            session_id, **fact_arguments(
                RoutinesUnwatched(action=action, routine_ids=list(routine_ids), text=text),
                delivery_id=f"absence:{session_id}:{action}:{identity}",
            ),
        )

    async def _audience(self, session_id: str) -> Audience:
        """A long-lived Agent's closing text is its work log; it reaches the person through message_user."""
        return "user" if await self.agents.by_session(session_id) is None else "log"

    async def _emit_schedule_changed(self, session_id: str) -> None:
        if self.schedule_changed_sink is None:
            return
        emitted = self.schedule_changed_sink(session_id)
        if isawaitable(emitted):
            await emitted

    async def _deliver_scheduled(
        self, schedule: ScheduledCheck, fired_at
    ) -> None:
        try:
            await self.request(
                "fact",
                schedule.session_id,
                {
                    "fact_type": TIME_REACHED,
                    "data": {
                        "schedule_id": schedule.schedule_id,
                        "purpose": schedule.purpose,
                        "planned_at": schedule.due_at.isoformat(),
                        "fired_at": fired_at.isoformat(),
                    },
                    "delivery_id": schedule.schedule_id,
                    "source": AUTOMATION_SOURCE,
                    "requests_decision": True,
                    "causation_id": schedule.schedule_id,
                },
            )
        except WorkerFailed as error:
            raise ScheduleDeliveryUnavailable(schedule.schedule_id) from error

    def next_scheduled_check(self, session_id: str) -> ScheduledCheck | None:
        return self.automation.schedules.next_pending(session_id)

    async def _start(self, session_id):
        path = self.store.require(session_id)
        events = await SqliteJournal(path).snapshot(session_id)
        is_subagent = project_parent(events) is not None
        reader_job = self.compact.store.reader_job(session_id)
        # Background compaction keeps the source's execution root and assembly.
        git_workspace = await self._git_workspace(session_id if reader_job is None else reader_job["source"])
        # close() may have taken its Worker snapshot while the Journal read waited.
        if self.closed:
            raise RuntimeError("Host closed")
        context = multiprocessing.get_context("spawn")
        local, remote = context.Pipe()
        worker = None

        async def signal(kind, *values):
            if kind == "timing":
                from redpanda.timing import emit_worker_timing
                emit_worker_timing(values[0])
            elif kind == "usage":
                if (
                    self.context_usage_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    emitted = self.context_usage_sink(*values)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "activity":
                if (
                    self.subagent_activity_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    self.subagent_activity_sink(
                        *values
                    )
            elif kind == "tool":
                _, phase, command_id, name, _ = values
                if phase == "start":
                    worker.live_tools[command_id] = name
                else:
                    worker.live_tools.pop(command_id, None)
                if (
                    self.tool_progress_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    emitted = self.tool_progress_sink(*values)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "authorization":
                if (
                    self.authorization_required_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    emitted = self.authorization_required_sink(*values)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "preview":
                if is_subagent and self.subagent_output_sink is None:
                    return
                _, phase, output_id, _ = values
                if phase == "started":
                    worker.active_preview = output_id
                elif phase == "aborted":
                    worker.active_preview = None
                if (
                    self.preview_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    audience = await self._audience(values[0])
                    await _emit(self.preview_sink(*values, audience))
            elif kind == "thinking":
                if is_subagent and self.subagent_output_sink is None:
                    return
                _, phase, output_id, _ = values
                if phase == "started":
                    worker.active_thinking = output_id
                elif phase in {"finished", "aborted"}:
                    worker.active_thinking = None
                if (
                    self.thinking_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    audience = await self._audience(values[0])
                    await _emit(self.thinking_sink(*values, audience))
            elif kind == "session_failed":
                await self._emit_session_failed(*values)
            elif kind == "idle":
                was_running = worker.running
                worker.idle_revision = values[0]
                worker.idle_has_active_subagents = values[1]
                worker.running = False
                worker.changed.set()
                await self._stop_idle(session_id, worker)
                if was_running:
                    await self._emit_session_activity(session_id, "idle")
            elif kind == "busy":
                self._current_failures.pop(session_id, None)
                worker.idle_revision = None
                worker.idle_has_active_subagents = False
                worker.running = True
                worker.stopping = False
                worker.transition.set()
                worker.changed.set()
                await self._emit_session_activity(session_id, "running")
            elif kind == "stopping":
                worker.stopping = True
                worker.changed.set()
            elif kind == "failure":
                worker.failure = WorkerFailed(session_id, values[0])
                worker.changed.set()
            elif kind == "return":
                worker.returned = tuple(values)
            else:
                raise ValueError(f"Unknown Host signal: {kind}")

        async def route(operation, target, arguments):
            if operation in {SEND_MESSAGE, SEND_GROUP_MESSAGE, LIST_COLLEAGUES, DECLARE_DIRECTION}                     and target != session_id:
                raise ValueError("communication request does not belong to this Worker")
            return await self._route(
                operation,
                target,
                arguments,
                worker=worker,
            )

        peer = PipePeer(local, route, signal, peer_alive=lambda: worker.process.is_alive())
        # Pipe handshake instead of Event: spawn children on some Linux
        # sandboxes cannot rebuild /dev/shm SemLocks (FileNotFoundError).
        admit_recv, admit_send = context.Pipe(duplex=False)
        process = context.Process(
            target=worker_main,
            args=(
                remote,
                session_id,
                path,
                self.config_factory,
                self.home.root,
                admit_recv,
                self.command_environment,
                git_workspace,
            ),
            name=f"session:{session_id}",
        )
        start_worker(process, extra_handles=(local, remote, admit_recv, admit_send))
        try:
            if self.job is not None:
                self.job.assign(process.pid)
            admit_send.send(None)
            admit_send.close()
        except BaseException:
            process.terminate()
            await asyncio.to_thread(process.join)
            process.close()
            local.close()
            remote.close()
            try:
                admit_send.close()
            except OSError:
                pass
            try:
                admit_recv.close()
            except OSError:
                pass
            raise
        remote.close()
        try:
            admit_recv.close()
        except OSError:
            pass
        worker = Worker(process, peer)
        self.workers[session_id] = worker
        worker.reader = asyncio.create_task(self._watch(session_id, worker))
        self._track(session_id, worker.reader)
        return worker

    def _track(self, session_id, task):
        self.watchers.add(task)

        def watched(task):
            self.watchers.remove(task)
            if (
                not task.cancelled()
                and task.exception() is not None
                and not isinstance(task.exception(), WorkerFailed)
                and session_id not in self.intentionally_stopped
            ):
                # WorkerFailed is already exposed by that Worker's lifecycle watcher.
                self.failures.put_nowait(
                    WorkerFailed(session_id, ProcessFailure.capture(task.exception()))
                )

        task.add_done_callback(watched)

    async def _watch(self, session_id, worker):
        try:
            try:
                await worker.peer.run()
            except (EOFError, ConnectionResetError, BrokenPipeError):
                # Pipe EOF is a process lifecycle fact, not a Command outcome.
                pass
            except Exception as error:
                # Host-side IPC or routing failure belongs to this Worker boundary.
                worker.failure = WorkerFailed(session_id, ProcessFailure.capture(error))
                worker.process.terminate()
            await asyncio.to_thread(worker.process.join)
            if (
                worker.failure is None
                and worker.process.exitcode != 0
                and not self.closed
                and not worker.intentionally_stopped
                and session_id not in self.intentionally_stopped
            ):
                worker.failure = WorkerFailed(
                    session_id,
                    ProcessFailure(
                        "ProcessExit",
                        f"exit code {worker.process.exitcode}",
                        "",
                    ),
                )
        finally:
            worker.stopping = True
            await self._close_generation_display(session_id, worker)
            await worker.peer.close(worker.failure or RuntimeError("Worker exited"))
            worker.peer.connection.close()
            worker.process.close()
            if self.workers.get(session_id) is worker:
                self.workers.pop(session_id)
            if self.models is not None:
                self.models.release_worker(session_id)
            worker.exited.set()
            worker.transition.set()
            worker.changed.set()
        # Release the dead Worker and its pending requests before waking the parent.
        if worker.failure is not None:
            self.compact.store.fail(session_id, asdict(worker.failure.failure))
            compact_job = self.compact.store.reader_job(session_id)
            if compact_job is None:
                self._current_failures[session_id] = f"Session 进程失败：{worker.failure.failure.render()}"
            if compact_job is not None:
                self.compact.notify_status(compact_job["source"])
            self.failures.put_nowait(worker.failure)
            if compact_job is not None and compact_job["published"] == 0 and not self.closed:
                self._track(
                    compact_job["source"],
                    asyncio.create_task(
                        self.request("compact_ready", compact_job["source"], {})
                    ),
                )
        if not self.closed and self.compact.store.job(session_id) is not None:
            events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
            if project_parent(events) is not None and project_returned(events):
                await self.compact.retire(session_id)
        if worker.returned is not None and not self.closed:
            parent, arguments = worker.returned
            await self.request("fact", parent, arguments)

    async def _close_generation_display(self, session_id, worker):
        """Retire only transient display state owned by this Worker generation."""

        if self.compact.store.reader_job(session_id) is not None:
            worker.active_preview = None
            worker.active_thinking = None
            worker.live_tools.clear()
            worker.running = False
            return
        output_id = worker.active_preview
        worker.active_preview = None
        if output_id is not None and self.preview_sink is not None:
            await _emit(self.preview_sink(session_id, "aborted", output_id, None,
                                          await self._audience(session_id)))
        thinking_id = worker.active_thinking
        worker.active_thinking = None
        if thinking_id is not None and self.thinking_sink is not None:
            await _emit(self.thinking_sink(session_id, "aborted", thinking_id, None,
                                           await self._audience(session_id)))
        live_tools = tuple(worker.live_tools.items())
        worker.live_tools.clear()
        if self.tool_progress_sink is not None:
            for command_id, name in live_tools:
                emitted = self.tool_progress_sink(
                    session_id,
                    "fail",
                    command_id,
                    name,
                    None,
                )
                if isawaitable(emitted):
                    await emitted
        if worker.running:
            worker.running = False
            await self._emit_session_activity(session_id, "idle")

    async def _reclaim_child(self, session_id, arguments):
        """Stop the child Worker, persist cancel if needed, then report to parent.

        Host writes the child's Journal only after that Session has no Worker.
        """

        parent_session_id = arguments["parent_session_id"]
        reason = arguments.get("reason")
        self.intentionally_stopped.add(session_id)
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            worker = self.workers.get(session_id)
            if worker is not None and not worker.exited.is_set():
                worker.intentionally_stopped = True
                if worker.process.is_alive():
                    worker.process.terminate()
                await worker.exited.wait()
            returned = await persist_return(
                SqliteJournal(self.store.require(session_id)),
                session_id,
                return_data(
                    session_id,
                    reported=False,
                    summary=None,
                    failure=None,
                    cancelled=True,
                    reason=reason,
                ),
            )
        await self.compact.retire(session_id)
        if worker is not None and worker.failure is not None:
            raise worker.failure
        await self.request(
            "fact",
            parent_session_id,
            report_arguments(session_id, returned.payload.data),
        )

    def _selected(self, session_id):
        return session_id in self.selections.values()

    def _being_selected(self, session_id):
        return session_id in self.selecting.values()

    async def _stop_idle(self, session_id, worker):
        if (
            worker.requests == 0
            and worker.idle_revision is not None
            and not worker.stopping
            and not self._selected(session_id)
            and not self._being_selected(session_id)
        ):
            worker.stopping = True
            worker.transition.clear()
            await worker.peer.send(("stop", worker.idle_revision))

    async def request(self, operation, session_id, arguments):
        assert not self.closed
        if operation == "fact" and arguments["fact_type"] == REPORT_FACT:
            prepared = await self._route("prepare_child_files", arguments["data"]["child_session_id"],
                                         {"parent_session_id": session_id})
            if not prepared["ok"] and prepared["code"] == "CHILD_STILL_WORKING":
                raise ValueError("child report precedes its durable return")
        lock = self.locks.setdefault(session_id, asyncio.Lock())
        just_started = False
        while True:
            async with lock:
                if self.closed:
                    raise RuntimeError("Host closed")
                worker = self.workers.get(session_id)
                if worker is None:
                    worker = await self._start(session_id)
                    just_started = True
                if not worker.stopping:
                    worker.requests += 1
                    worker.idle_revision = None
                    break
            await worker.transition.wait()
        try:
            if just_started and operation != "apply_auto_authorize":
                await worker.peer.request(
                    "apply_auto_authorize",
                    session_id,
                    {"enabled": self.auto_authorize(session_id)},
                )
            return await worker.peer.request(operation, session_id, arguments)
        finally:
            worker.requests -= 1
            if not worker.exited.is_set():
                await self._stop_idle(session_id, worker)

    def auto_authorize(self, session_id):
        return self._auto_authorize.get(session_id)

    def is_paused(self, session_id):
        return self._pause.get(session_id)

    def _with_host_metadata(self, view, session_id):
        return replace(
            view,
            auto_authorize=self.auto_authorize(session_id),
            paused=self.is_paused(session_id),
            last_error=self.current_failure(session_id),
        )

    def current_failure(self, session_id):
        return self._current_failures.get(session_id)

    async def _push_auto_authorize(self, session_id):
        return await self.request(
            "apply_auto_authorize",
            session_id,
            {"enabled": self.auto_authorize(session_id)},
        )

    def conversation_status(self, session_id):
        return self.compact.store.status(session_id)

    def activity(self, session_id):
        worker = self.workers.get(session_id)
        if worker is not None and worker.running:
            return "running"
        return "idle"

    async def _emit_session_activity(self, session_id, activity):
        if (
            self.session_activity_sink is None
            or self.compact.store.reader_job(session_id) is not None
        ):
            return
        emitted = self.session_activity_sink(session_id, activity)
        if isawaitable(emitted):
            await emitted

    async def _emit_session_failed(self, session_id, message):
        if self.compact.store.reader_job(session_id) is not None:
            return
        self._current_failures[session_id] = message
        if self.session_failed_sink is None:
            return
        emitted = self.session_failed_sink(session_id, message)
        if isawaitable(emitted):
            await emitted

    async def bound_workspace_id(self, session_id: str) -> str:
        """ä¼è¯èªå·±çå½å±ï¼æ´¾çå­ä¼è¯ä¸åç¼© reader é½ç»§æ¿å®ã"""
        events = await SqliteJournal(self.store.require(session_id)).snapshot(
            session_id
        )
        workspace_id = bound_workspace_id(events)
        if workspace_id is None:
            raise UnboundSessionError(session_id)
        return workspace_id

    async def create(self, session_id, workspace_id):
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            self.workspaces.get(workspace_id)
            await self.store.create(session_id, workspace_id=workspace_id)
            if self.models is not None:
                self.models.initialize_session(session_id)

    async def create_agent(
        self, *, name: str, workspace_id: str,
    ) -> AgentRecord:
        """Create a project-owned Git worktree before publishing the Agent fact."""
        project = self.workspaces.get(workspace_id)
        if self.models is not None:
            self.models.require_default_ready()
        session_id = f"agent-{uuid4().hex}"
        worktree = self.agent_worktrees.agent(workspace_id, session_id)
        identity = AgentCreated(name=name, workspace_id=workspace_id,
                                worktree=str(worktree.path), branch=worktree.branch)
        async with self._agent_creation_lock:
            for entry in await self.agents.list(workspace_id):
                if entry.agent.name == name:
                    pending = self.store.require(entry.agent.session_id).parent / "agent-creation.json"
                    if pending.exists():
                        return await _settle(self._finish_agent_creation(entry.agent))
                    raise AgentNameTaken(f"项目内已有 Agent：{name}")
            # A cancelled request must not leave a worktree without its identity fact.
            return await _settle(self._create_agent(project, session_id, identity))

    async def _create_agent(self, project, session_id, identity) -> AgentRecord:
        colleagues = tuple(entry.agent.session_id for entry in await self.agents.list(project.workspace_id))
        worktree = await asyncio.to_thread(self.agent_worktrees.create, project, session_id)
        try:
            await self.store.create(
                session_id, workspace_id=project.workspace_id,
                initial_fact=fact_arguments(identity, delivery_id="created"),
                agent_colleagues=colleagues,
            )
        except BaseException as error:
            try:
                await asyncio.to_thread(self.agent_worktrees.remove, project, worktree)
            except BaseException as cleanup_error:
                raise BaseExceptionGroup("Agent registration and worktree cleanup failed", [error, cleanup_error]) from None
            raise
        agent = AgentRecord(session_id, identity)
        # The identity is durable now; the roster must know it before anything else can fail,
        # or a retry would publish a second Agent under the same name.
        await self.agents.add(agent)
        return await self._finish_agent_creation(agent)

    async def _finish_agent_creation(self, agent: AgentRecord) -> AgentRecord:
        session_id, identity = agent.session_id, agent.identity
        pending = self.store.require(session_id).parent / "agent-creation.json"
        colleagues = json.loads(pending.read_text(encoding="utf-8"))["colleagues"]
        if self.models is not None:
            self.models.initialize_session(session_id)
        # Colleagues learn of the join before anything the new Agent does;
        # replaying its creation fact then starts the Worker for its first decision.
        await self.agent_messages.joined(agent, colleagues)
        await self.request("fact", session_id, fact_arguments(identity, delivery_id="created"))
        pending.unlink()
        return agent

    async def select(self, owner, session_id):
        async with self.selection_locks.setdefault(owner, asyncio.Lock()):
            self.store.require(session_id)
            previous = self.selections.get(owner)
            self.selecting[owner] = session_id
            self.selections[owner] = session_id
            try:
                view = await self._select_view(session_id)
            except BaseException:
                if previous is None:
                    self.selections.pop(owner, None)
                else:
                    self.selections[owner] = previous
                raise
            finally:
                del self.selecting[owner]
                worker = self.workers.get(session_id)
                if worker is not None:
                    await self._stop_idle(session_id, worker)
            if previous is not None and previous != session_id:
                worker = self.workers.get(previous)
                if worker is not None:
                    await self._stop_idle(previous, worker)
            return view

    async def _journal_state(self, session_id):
        events = await SqliteJournal(self.store.require(session_id)).snapshot(
            session_id
        )
        return events, replay(session_id, events).state

    async def _project_view(self, session_id):
        events, state = await self._journal_state(session_id)
        return self._with_host_metadata(
            session_view(
                state,
                control_approval=pending_approval_view(events),
                control_message=project_control_message(events),
                has_active_subagents=bool(project_pending(events)),
            ),
            session_id,
        )

    async def _should_wake(self, session_id):
        if self.is_paused(session_id) or self.current_failure(session_id) is not None:
            return False
        _events, state = await self._journal_state(session_id)
        return session_view(state).should_wake

    async def _select_view(self, session_id):
        if await self._should_wake(session_id):
            return await self.resume(session_id)
        return await self._project_view(session_id)

    async def release(self, owner):
        async with self.selection_locks.setdefault(owner, asyncio.Lock()):
            session_id = self.selections.pop(owner, None)
            if session_id is None:
                return
            worker = self.workers.get(session_id)
            if worker is not None:
                await self._stop_idle(session_id, worker)

    async def resume(self, session_id):
        operation = "view" if self.is_paused(session_id) else "resume"
        return self._with_host_metadata(
            await self.compact.application(operation, session_id, {}),
            session_id,
        )

    async def retry(self, session_id):
        if self.is_paused(session_id):
            self._pause.set(session_id, False)
        return await self.resume(session_id)

    async def view(self, session_id):
        return self._with_host_metadata(
            await self.compact.application("view", session_id, {}),
            session_id,
        )

    async def receive_user_message(self, session_id, content, **kwargs):
        async with self.turn_locks.setdefault(session_id, asyncio.Lock()):
            if self.models is not None:
                self.store.require(session_id)
                self.models.require_ready(session_id)
            if self._pause.get(session_id):
                self._pause.set(session_id, False)
            await self.compact.application(
                "receive_user_message", session_id, dict(content=content, **kwargs)
            )

    async def accept_input(self, session_id, content, **kwargs):
        async with self.turn_locks.setdefault(session_id, asyncio.Lock()):
            if self.models is not None:
                self.store.require(session_id)
                self.models.require_ready(session_id)
            if self._pause.get(session_id):
                self._pause.set(session_id, False)
            view = await self.compact.application(
                "accept_input", session_id, dict(content=content, **kwargs)
            )
            return self._with_host_metadata(view, session_id)

    async def resolve_authorization(self, session_id, command_id, *, approved):
        await self.compact.application(
            "resolve_authorization",
            session_id,
            dict(command_id=command_id, approved=approved),
        )

    async def resolve_authorizations(self, session_id, *, approved):
        await self.compact.application(
            "resolve_authorizations", session_id, dict(approved=approved)
        )

    async def set_auto_authorize(self, session_id, enabled):
        self._auto_authorize.set(session_id, enabled)
        view = await self._push_auto_authorize(session_id)
        return self._with_host_metadata(view, session_id)

    def model_selection(self, session_id):
        self.store.require(session_id)
        return self.models.selection(session_id)

    def set_model(self, session_id, model):
        self.store.require(session_id)
        return self.models.select(session_id, model)

    async def resolve_control(self, session_id, request_id, *, approved):
        return await self.compact.application(
            "resolve_control",
            session_id,
            dict(request_id=request_id, approved=approved),
        )

    async def cancel_turn(self, session_id):
        async with self.turn_locks.setdefault(session_id, asyncio.Lock()):
            return await self._cancel_turn(session_id, "用户中断父会话")

    async def interrupt_for_group(self, session_id):
        """A stopped group discussion interrupts its called member without parking it.

        The member stays reachable: later messages wake it as usual, and whatever
        the interruption left to decide is decided now.
        """
        async with self.turn_locks.setdefault(session_id, asyncio.Lock()):
            await self._cancel_turn(session_id, "群讨论已被停止")
            self._pause.set(session_id, False)
            return await self._select_view(session_id)

    async def _cancel_turn(self, session_id, reason):
        # Child return is a new external fact, even when it races with reclaim.
        # Keep the stopped parent paused until an explicit continuation/input.
        self._pause.set(session_id, True)
        await self.compact.application("cancel_turn", session_id, {})
        # Wait for this Worker's direct commands to settle before projecting
        # children: a committed delegate may still be creating its Session.
        worker = self.workers.get(session_id)
        if worker is not None:
            while True:
                worker.changed.clear()
                if worker.failure is not None:
                    raise worker.failure
                if worker.idle_revision is not None or worker.exited.is_set():
                    break
                await worker.changed.wait()
        stopped = await asyncio.gather(*(
            self._reclaim_child(
                child_id,
                {"parent_session_id": session_id, "reason": reason},
            )
            for child_id in await self._running_children(session_id)
        ), return_exceptions=True)
        errors = [result for result in stopped if isinstance(result, BaseException)]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("收回子任务失败", errors)
        return await self.wait_quiescent(session_id)

    async def wait_quiescent(self, session_id):
        worker = self.workers.get(session_id)
        if worker is None:
            return await self.view(session_id)
        while True:
            worker.changed.clear()
            if worker.failure is not None:
                raise worker.failure
            if worker.idle_revision is not None and (
                not worker.idle_has_active_subagents
                or not await self._children_still_running(session_id)
            ):
                return await self.view(session_id)
            if worker.exited.is_set():
                return await self.view(session_id)
            if worker.changed.is_set():
                continue
            await worker.changed.wait()

    async def _children_still_running(self, session_id) -> bool:
        """待回收集合里，子 Journal 还没有回传的那些才算还在工作。

        子会话自己的回传已经说明它停了，不必等父会话记下回收。
        """

        return bool(await self._running_children(session_id))

    async def _running_children(self, session_id):
        events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
        running = []
        for child_id in project_pending(events):
            if not self.store.path(child_id).parent.exists():
                running.append(child_id)
                continue
            child_events = await SqliteJournal(self.store.require(child_id)).snapshot(child_id)
            if not project_returned(child_events):
                running.append(child_id)
        return tuple(running)

    async def wait_failure(self):
        return await self.failures.get()

    async def close(self):
        for task in (self._automation_task, self._routine_task, self._absence_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.closed = True
        workers = tuple(self.workers.values())
        for worker in workers:
            if worker.process.is_alive():
                worker.process.terminate()
        await asyncio.gather(*self.watchers, return_exceptions=True)
        if self.processes is not None:
            await self.processes.close()
        if self.job is not None:
            self.job.close()
            self.job = None
