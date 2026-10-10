from __future__ import annotations

import asyncio
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import threading

from redpanda.automation.recovery import recover_schedule_attempts
from redpanda.assistant.assembly import build_assistant_assembly
from redpanda.assistant.host.ipc import PipePeer, ProcessFailure
from redpanda.assistant.host.llm_port import WorkerLlmPort
from redpanda.assistant.subagent.subagent import (
    project_parent, record_interrupted_return, record_unexpected_return,
)
from redpanda.assistant.workspaces import bound_workspace
from redpanda.paths import RedPandaHome
from redpanda.runtime import SqliteJournal
from redpanda.sandbox.registry import WorkspaceRegistry


async def run_worker(connection, session_id, path, config_factory, home_root, command_environment):
    journal = SqliteJournal(path)
    try:
        await _run_session(connection, session_id, journal, config_factory, home_root, command_environment)
    except Exception as error:
        try:
            returned = await record_unexpected_return(journal, session_id, error)
            if returned is not None:
                # One-way handoff: no dependency on the failed Worker's reader.
                await asyncio.to_thread(connection.send, ("return", *returned))
        except Exception as reporting_error:
            raise ExceptionGroup(
                "Worker failure and return handoff failure", [error, reporting_error]
            ) from None
        raise


async def _run_session(connection, session_id, journal, config_factory, home_root, command_environment):
    # Each process owns execution clients, caches and its single Journal.
    # The model implementation lives on the Host; this Worker only holds a Port.
    home = RedPandaHome(Path(home_root))
    await journal.prepare_recovery(session_id)
    await record_interrupted_return(journal, session_id)
    config = config_factory()
    events = await journal.snapshot(session_id)
    workspace = bound_workspace(
        session_id,
        events,
        WorkspaceRegistry.load(home.workspaces_path),
    )
    stop = asyncio.Event()
    active_requests = 0
    ready = asyncio.Event()
    revision = 0
    advertised = -1
    advertised_busy = False

    async def handle(operation, target, arguments):
        nonlocal revision, active_requests
        assert target == session_id
        await ready.wait()
        active_requests += 1
        revision += 1
        try:
            if operation == "compact_publish":
                return await assembly.compact.publish(arguments)
            if operation == "compact_snapshot":
                return await assembly.compact.snapshot(**arguments)
            if operation == "compact_ready":
                await assembly.scheduler.wake(session_id)
                return None
            if operation == "fact":
                await assembly.runtime.receive_domain_fact(session_id, **arguments)
                parent = project_parent(await journal.snapshot(session_id))
                if parent is not None:
                    assembly.subagents._parents[session_id] = parent
                await assembly.subagents.refresh_activity(session_id)
                if await assembly.subagents.note_returned(session_id):
                    return None
                await assembly.scheduler.wake(session_id)
                result = None
            elif operation == "create":
                result = await assembly.sessions.view(session_id)
            else:
                result = await getattr(assembly.sessions, operation)(
                    session_id, **arguments
                )
            return result
        finally:
            active_requests -= 1
            assembly.scheduler.changed.set()

    async def signal(kind, *payload):
        nonlocal advertised
        if kind != "stop":
            raise ValueError(f"Unknown Worker signal: {kind}")
        (observed_revision,) = payload
        if (
            observed_revision == revision
            and active_requests == 0
            and assembly.scheduler.idle
        ):
            stop.set()
        else:
            advertised = -1
            await peer.send(("busy",))
            assembly.scheduler.changed.set()

    peer = PipePeer(connection, handle, signal)
    config = replace(config, llm=WorkerLlmPort(peer, session_id))

    async def model_selection_source(apply):
        return await peer.request("model_settings", session_id, {"apply": apply})

    async def sink(target, output_id, text):
        await peer.request(
            "output",
            target,
            {"output_id": output_id, "text": text},
        )

    async def preview_sink(target, phase, output_id, text):
        await peer.send(("preview", target, phase, output_id, text))

    async def subagent_output_sink(target, output_id, text):
        await peer.request("subagent_output", target, {"output_id": output_id, "text": text})

    async def thinking_sink(target, phase, output_id, text):
        await peer.send(("thinking", target, phase, output_id, text))

    # These callbacks only affect display; queue their IPC in the same event loop.
    notifications: set[asyncio.Task] = set()

    def notify(kind, *values):
        task = asyncio.create_task(peer.send((kind, *values)))
        notifications.add(task)

        def completed(done):
            notifications.discard(done)
            if not done.cancelled() and done.exception() is not None:
                peer.failure = done.exception()

        task.add_done_callback(completed)

    assembly = await build_assistant_assembly(
        config,
        sink,
        journal,
        session_id=session_id,
        workspace=workspace,
        command_environment=command_environment,
        context_usage_sink=lambda *values: notify("usage", *values),
        subagent_activity_sink=lambda *values: notify("activity", *values),
        tool_progress_sink=lambda *values: notify("tool", *values),
        authorization_required_sink=lambda *values: notify("authorization", *values),
        session_failed_sink=lambda *values: notify("session_failed", *values),
        preview_sink=preview_sink,
        thinking_sink=thinking_sink,
        subagent_output_sink=subagent_output_sink,
        session_transport=peer.request,
        model_selection_source=model_selection_source,
        home=home,
    )
    config.llm.bind_attachment_reader(assembly.read_attachment)
    async with config.llm, assembly.mcp.client_manager:
        reader = asyncio.create_task(peer.run())
        stopped = asyncio.create_task(stop.wait())
        try:
            # Rebuild only this Session. An explicit resume separately resumes its children.
            from redpanda.assistant.runner import resume_session

            await recover_schedule_attempts(journal, session_id, peer.request)
            await resume_session(
                assembly.runtime,
                assembly.surface,
                session_id,
                assembly.sessions._management,
                assembly.catalog,
            )
            await assembly.sessions.recover_control(session_id)
            parent = project_parent(events)
            if parent is not None:
                assembly.subagents._parents[session_id] = parent
            ready.set()
            while not stop.is_set():
                changed = asyncio.create_task(assembly.scheduler.changed.wait())
                done, _ = await asyncio.wait(
                    (reader, stopped, changed), return_when=asyncio.FIRST_COMPLETED
                )
                if not changed.done():
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
                if reader in done:
                    await reader
                if assembly.scheduler._failure is not None:
                    raise assembly.scheduler._failure
                assembly.scheduler.changed.clear()
                if not assembly.scheduler.idle:
                    if not advertised_busy:
                        advertised_busy = True
                        advertised = -1
                        await peer.send(("busy",))
                    continue
                if (
                    active_requests == 0
                    and advertised != revision
                ):
                    view = await assembly.sessions.view(session_id)
                    advertised = revision
                    advertised_busy = False
                    await peer.send(
                        (
                            "idle",
                            revision,
                            view.has_active_subagents,
                        )
                    )
            await peer.send(("stopping",))
        finally:
            await peer.close(RuntimeError("Worker closed"))
            reader.cancel()
            stopped.cancel()
            await asyncio.gather(reader, stopped, return_exceptions=True)
            await assembly.scheduler.close()
            if notifications:
                await asyncio.gather(*notifications)


def worker_main(connection, session_id, path, config_factory, home_root, admitted, command_environment):
    def exit_with_parent():
        multiprocessing.parent_process().join()
        os._exit(1)

    threading.Thread(target=exit_with_parent, daemon=True).start()
    try:
        # admitted is a one-shot Pipe end from the Host (was Event; SemLock
        # rebuild breaks under some Linux sandbox /dev/shm setups).
        admitted.recv()
        admitted.close()
        asyncio.run(run_worker(connection, session_id, path, config_factory, home_root, command_environment))
    except BaseException as error:
        # Process boundary: transport original diagnostics, then let the process fail.
        connection.send(("failure", ProcessFailure.capture(error)))
        raise
    finally:
        connection.close()
