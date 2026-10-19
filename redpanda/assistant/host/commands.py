"""Host command ownership, durable completion, and Journal-backed delivery."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from redpanda.assistant.agent_git import identity as git_identity
from redpanda.assistant.commands import COMMAND_FINISHED, SOURCE, background_commands, completed_commands
from redpanda.assistant.host.ipc import WorkerFailed
from redpanda.execution.command import CommandStartError, ShellNotFoundError
from redpanda.execution.processes import CommandManager
from redpanda.runtime import CommandOutcomeReceived, SqliteJournal


class CommandHost:
    def __init__(self, host):
        self.host = host
        self.started_at = datetime.now(timezone.utc)
        self.manager = CommandManager(host.home.state_root / "commands", host.command_environment)
        self.deliveries: dict[str, asyncio.Task] = {}

    def watch(self, session_id, initiating_id, identity):
        if identity not in self.deliveries:
            task = asyncio.create_task(self._deliver(session_id, initiating_id, identity))
            self.deliveries[identity] = task
            self.host._track(session_id, task)

    async def restore(self, session_id, events):
        delivered = completed_commands(events)
        for initiating_id, reference in background_commands(events):
            await self.manager.restore(session_id, reference)
            if reference["command_id"] not in delivered:
                self.watch(session_id, initiating_id, reference["command_id"])

    async def route(self, operation, session_id, arguments):
        self.host.store.require(session_id)
        identity = arguments.get("command_id")
        if operation == "command_execute":
            workspace = await self.host._git_workspace(session_id)
            try:
                result = await self.manager.execute(
                    session_id, identity, arguments["command"], Path(arguments["cwd"]), arguments["wait_seconds"],
                    environment_overlay=None if workspace is None else git_identity(workspace.author),
                )
            except ShellNotFoundError as exc:
                return {"ok": False, "code": "SHELL_NOT_FOUND", "shell": exc.shell_name,
                        "executable": exc.executable}
            except CommandStartError as exc:
                return {"ok": False, "code": "COMMAND_START_FAILED", "error": str(exc)}
            if result["backgrounded"]:
                self.watch(session_id, arguments["initiating_id"], identity)
            return {"ok": True, "command": result}
        if operation == "command_query":
            result = await self.manager.lookup(session_id, identity)
            if result is None:
                status = "not_started" if datetime.fromisoformat(arguments["started_at"]) >= self.started_at else "unknown"
                return {"status": status}
            if result["state"] == "running":
                self.manager.background(session_id, identity)
                result["backgrounded"] = True
                self.watch(session_id, arguments["initiating_id"], identity)
            else:
                # This is recovery of an uncommitted foreground Outcome.
                result["backgrounded"] = False
            return {"status": "found", "command": result}
        if operation == "command_read":
            return await self.manager.read(session_id, identity, arguments["stdout_offset"], arguments["stderr_offset"])
        if operation == "command_stop":
            return await self.manager.stop(session_id, identity)
        if operation == "command_interrupt":
            await self.manager.interrupt(session_id, identity)
            return None
        if operation == "command_list":
            return await self.manager.list(session_id)
        raise ValueError(operation)

    async def _deliver(self, session_id, initiating_id, identity):
        journal = SqliteJournal(self.host.store.require(session_id))
        # The process may finish before the Worker commits the handoff Outcome.
        while True:
            events = await journal.snapshot(session_id)
            if identity in completed_commands(events):
                return
            if any(reference["command_id"] == identity for _, reference in background_commands(events)):
                break
            if any(isinstance(event.payload, CommandOutcomeReceived)
                   and event.payload.command_id == initiating_id for event in events):
                return
            await asyncio.sleep(.1)
        result = await self.manager.wait(session_id, identity)
        data = {key: value for key, value in result.items() if key != "backgrounded"}
        if result["state"] == "unknown":
            data["notice"] = "应用已重新启动，命令不再运行且不会重启；没有退出记录，可能已部分执行，请先核对现场。"
        while True:
            try:
                await self.host.request("fact", session_id, {
                    "source": SOURCE, "fact_type": COMMAND_FINISHED, "delivery_id": identity,
                    "requests_decision": True, "causation_id": initiating_id, "data": data,
                })
            except WorkerFailed:
                # Journal acceptance is idempotent; retry delivery, never execution.
                await asyncio.sleep(30)
            else:
                return

    async def close(self):
        for task in self.deliveries.values():
            task.cancel()
        await asyncio.gather(*self.deliveries.values(), return_exceptions=True)
        await self.manager.close()
