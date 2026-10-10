from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
from inspect import isawaitable
import multiprocessing
import os
from datetime import datetime, timezone

from redpanda.automation.once import (
    SOURCE as AUTOMATION_SOURCE,
    TIME_REACHED,
    OneShotClock,
    OneShotSchedules,
    ScheduleDeliveryUnavailable,
    ScheduledCheck,
)
from redpanda.assistant.control import pending_approval_view, project_control_message
from redpanda.assistant.session_metadata import (
    SessionFlagStore,
    SessionLineageStore,
    SessionTextStore,
)
from redpanda.assistant.workspace_versions import (
    StepNotRewindable,
    WorkspaceVersionFact,
    workspace_version_event,
)
from redpanda.assistant.compact.host import CompactHost
from redpanda.assistant.delivery import emit_delivery
from redpanda.assistant.host.ipc import PipePeer, ProcessFailure, WorkerFailed
from redpanda.assistant.host.llm_port import complete_llm_chat
from redpanda.assistant.host.session_store import SessionStore
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
from redpanda.sandbox.child_files import ChildFiles
from redpanda.assistant.workspaces import UnboundSessionError, bound_workspace_id
from redpanda.assistant.host.spawn import start_worker
from redpanda.assistant.host.worker import worker_main
from redpanda.runtime import SqliteJournal, replay, DomainFactCommitted
from redpanda.sandbox.local.windows_job import WindowsJob
from redpanda.sandbox.registry import WorkspaceRegistry


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
        self.workers: dict[str, Worker] = {}
        self.watchers: set[asyncio.Task] = set()
        self.locks: dict[str, asyncio.Lock] = {}
        self.failures: asyncio.Queue[WorkerFailed] = asyncio.Queue()
        self.intentionally_stopped: set[str] = set()
        self.selections: dict[str, str] = {}
        self.selecting: dict[str, str] = {}
        self.selection_locks: dict[str, asyncio.Lock] = {}
        self.closed = False
        self.compact = CompactHost(self)
        self.job = WindowsJob.create() if os.name == "nt" else None
        self._auto_authorize = SessionFlagStore(store.root, "auto_authorize.json")
        self._pause = SessionFlagStore(store.root, "paused.json")
        self._archived = SessionFlagStore(store.root, "archived.json")
        self._titles = SessionTextStore(store.root, "titles.json")
        self._lineage = SessionLineageStore(store.root, "lineage.json")
        self.automation = OneShotClock(
            OneShotSchedules(home.state_root / "automation.sqlite")
        )
        self._automation_task: asyncio.Task[None] | None = None
        self.workspaces = (
            workspaces
            if workspaces is not None
            else WorkspaceRegistry.load(home.workspaces_path)
        )

    async def pause_unfinished_sessions(self) -> None:
        """进程刚起来时，还会自己往下走的顶层会话先停住。

        选中一条 RUNNABLE 会话会把它的 Worker 拉起来接着跑，子会话也跟着恢复。
        暂停留在 Host 元数据里，不改 Journal。等人点继续再醒。等外部事实的会话
        本来就不会自己动，子会话只由父恢复时带起来，这里都不改。
        """

        for path in self.store.journals():
            journal = SqliteJournal(path)
            session_id = await journal.session_identity()
            if self.is_paused(session_id):
                continue
            events = await journal.snapshot(session_id)
            if project_parent(events) is not None:
                continue
            if session_view(replay(session_id, events).state).should_wake:
                self._pause.set(session_id, True)

    async def _route(self, operation, session_id, arguments, *, worker=None):
        if operation == "model_settings":
            if self.models is not None:
                return self.models.for_decision(session_id, apply=arguments["apply"])
            return None
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
                "due_at": schedule.due_at.isoformat(),
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
                await emit_delivery(sink, session_id, arguments["output_id"], arguments["text"])
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
                await ChildFiles(self.home, parent_workspace).create(
                    expected_task.parent_session_id, session_id, conflict_child_id=conflict_child_id
                )
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
            workspace = self.workspaces.get(await self.bound_workspace_id(session_id))
            await ChildFiles(self.home, workspace).finish(task.parent_session_id, session_id)
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
        is_subagent = project_parent(await SqliteJournal(path).snapshot(session_id)) is not None
        context = multiprocessing.get_context("spawn")
        local, remote = context.Pipe()
        worker = None

        async def signal(kind, *values):
            if kind == "usage":
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
                    emitted = self.preview_sink(*values)
                    if isawaitable(emitted):
                        await emitted
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
                    emitted = self.thinking_sink(*values)
                    if isawaitable(emitted):
                        await emitted
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
            return await self._route(
                operation,
                target,
                arguments,
                worker=worker,
            )

        peer = PipePeer(local, route, signal, peer_alive=lambda: worker.process.is_alive())
        admitted = context.Event()
        process = context.Process(
            target=worker_main,
            args=(
                remote,
                session_id,
                path,
                self.config_factory,
                self.home.root,
                admitted,
                self.command_environment,
            ),
            name=f"session:{session_id}",
        )
        start_worker(process, extra_handles=(local, remote))
        try:
            if self.job is not None:
                self.job.assign(process.pid)
            admitted.set()
        except BaseException:
            process.terminate()
            await asyncio.to_thread(process.join)
            process.close()
            local.close()
            remote.close()
            raise
        remote.close()
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
            emitted = self.preview_sink(session_id, "aborted", output_id, None)
            if isawaitable(emitted):
                await emitted
        thinking_id = worker.active_thinking
        worker.active_thinking = None
        if thinking_id is not None and self.thinking_sink is not None:
            emitted = self.thinking_sink(session_id, "aborted", thinking_id, None)
            if isawaitable(emitted):
                await emitted
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
            if not prepared["ok"]:
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

    def is_superseded(self, session_id):
        return self._lineage.is_superseded(session_id)

    def is_archived(self, session_id):
        return self._archived.get(session_id)

    def session_title(self, session_id):
        return self._titles.get(session_id)

    def session_titles(self):
        return self._titles.items()

    async def archive(self, session_id):
        self.store.require(session_id)
        self._pause.set(session_id, True)
        if session_id in self.workers:
            await self.compact.application("cancel_turn", session_id, {})
        events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
        intents = project_delegate_intents(events)
        for intent in intents:
            if self.store.path(intent.child_session_id).exists():
                await self._reclaim_child(intent.child_session_id,
                                          {"parent_session_id": session_id, "reason": "父会话归档"})
        if intents:
            await self.wait_quiescent(session_id)
            workspace = self.workspaces.get(await self.bound_workspace_id(session_id))
            await ChildFiles(self.home, workspace).discard(session_id, [i.child_session_id for i in intents])
        self._archived.set(session_id, True)

    def set_title(self, session_id, title):
        self.store.require(session_id)
        self._titles.set(session_id, title)

    def _with_host_metadata(self, view, session_id):
        return replace(
            view,
            auto_authorize=self.auto_authorize(session_id),
            paused=self.is_paused(session_id),
        )

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
        if (
            self.session_failed_sink is None
            or self.compact.store.reader_job(session_id) is not None
        ):
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

    async def fork_and_accept_input(
        self,
        owner,
        source_session_id,
        message_id,
        edited_text,
        *,
        child_session_id,
        delivery_id,
        artifact_refs,
        source="user",
        listed=False,
        restore_files=False,
    ):
        async with self.locks.setdefault(source_session_id, asyncio.Lock()):
            await self.store.fork_before_message(
                source_session_id,
                message_id,
                child_session_id,
            )
            if self.models is not None:
                self.models.initialize_session(child_session_id, parent=source_session_id)
        # ???????????????????????????????
        if not listed:
            self._lineage.supersede(child_session_id, source_session_id)
        await self.select(owner, child_session_id)
        # ????????????????????????????????
        await self.compact.application(
            "settle_forked_workspace",
            child_session_id,
            dict(restore=bool(restore_files), delivery_id=f"{delivery_id}-workspace"),
        )
        return await self.accept_input(
            child_session_id,
            edited_text,
            delivery_id=delivery_id,
            source=source,
            artifact_refs=artifact_refs,
        )

    async def branch_after_turn(
        self, owner, source_session_id, user_message_id, child_session_id
    ):
        """从这一轮收口之后切一条新会话线。

        前缀含这一轮的全部回复，不含之后的用户消息。新身份自己成一条线，
        不顶掉来源，也不写新的用户消息——人在新会话的输入框里接着说。
        """
        async with self.locks.setdefault(source_session_id, asyncio.Lock()):
            await self.store.fork_after_turn(
                source_session_id, user_message_id, child_session_id
            )
            if self.models is not None:
                self.models.initialize_session(child_session_id, parent=source_session_id)
        await self.select(owner, child_session_id)
        return self._with_host_metadata(
            await self.compact.application("view", child_session_id, {}),
            child_session_id,
        )

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
        if self.is_paused(session_id):
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
            return await self.set_paused(session_id, False)
        return await self.resume(session_id)

    async def view(self, session_id):
        return self._with_host_metadata(
            await self.compact.application("view", session_id, {}),
            session_id,
        )

    async def receive_user_message(self, session_id, content, **kwargs):
        if self.models is not None:
            self.store.require(session_id)
            self.models.require_ready(session_id)
        if self._pause.get(session_id):
            self._pause.set(session_id, False)
        await self.compact.application(
            "receive_user_message", session_id, dict(content=content, **kwargs)
        )

    async def accept_input(self, session_id, content, **kwargs):
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

    async def set_paused(self, session_id, paused):
        self._pause.set(session_id, bool(paused))
        if paused:
            return self._with_host_metadata(
                await self.compact.application("view", session_id, {}),
                session_id,
            )
        return await self.resume(session_id)

    async def resolve_control(self, session_id, request_id, *, approved):
        return await self.compact.application(
            "resolve_control",
            session_id,
            dict(request_id=request_id, approved=approved),
        )

    async def cancel_turn(self, session_id):
        return self._with_host_metadata(
            await self.compact.application("cancel_turn", session_id, {}),
            session_id,
        )

    async def restart_from_step(
        self, owner, session_id, step_id, child_session_id, delivery_id, *, restore_files=True
    ):
        """人点时间线上的一步：从那一刻重开。

        只退文件是残缺的——会话照样停在第十步，那七步基于旧世界的推理还在
        上下文里，模型下次读文件会发现和自己的历史对不上。所以回退的载体是
        会话本身：截到那一步，文件跟着回去，两边描述同一个世界。

        先停源会话再切。跑着的命令会往共享工作树里写，也会继续往源 Journal
        追加事件。停下来不是副作用，是这个动作的一部分——人接着通常要换一条
        走法。
        """
        events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
        boundary = workspace_version_event(events, step_id)
        if boundary is None or WorkspaceVersionFact.parse(boundary.payload.data).version is None:
            raise StepNotRewindable(step_id)

        self._pause.set(session_id, True)
        await self.compact.application("cancel_turn", session_id, {})
        await self.wait_quiescent(session_id)

        async with self.locks.setdefault(session_id, asyncio.Lock()):
            await self.store.fork_after_event(session_id, boundary.event_id, child_session_id)
            if self.models is not None:
                self.models.initialize_session(child_session_id, parent=session_id)
        self._lineage.supersede(child_session_id, session_id)
        # 新身份带着完整历史停在那一刻，不许自己往下走，等人给下一句话。
        self._pause.set(child_session_id, True)
        await self.select(owner, child_session_id)
        await self.compact.application(
            "settle_forked_workspace",
            child_session_id,
            dict(restore=bool(restore_files), delivery_id=f"{delivery_id}-workspace"),
        )
        return self._with_host_metadata(
            await self.compact.application("view", child_session_id, {}),
            child_session_id,
        )

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

        分支可能继承了委派、却没有后来写到来源会话上的回收事实。
        子会话自己的回传已经说明它停了，再等那份回收不会到来。
        """

        events = await SqliteJournal(self.store.require(session_id)).snapshot(session_id)
        for child_id in project_pending(events):
            if not self.store.path(child_id).parent.exists():
                return True
            child_events = await SqliteJournal(self.store.require(child_id)).snapshot(child_id)
            if not project_returned(child_events):
                return True
        return False

    async def wait_failure(self):
        return await self.failures.get()

    async def close(self):
        if self._automation_task is not None:
            self._automation_task.cancel()
            await asyncio.gather(self._automation_task, return_exceptions=True)
        self.closed = True
        workers = tuple(self.workers.values())
        for worker in workers:
            if worker.process.is_alive():
                worker.process.terminate()
        await asyncio.gather(*self.watchers, return_exceptions=True)
        if self.job is not None:
            self.job.close()
            self.job = None
