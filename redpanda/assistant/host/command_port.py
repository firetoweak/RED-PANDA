"""Session-bound command execution and output access through the Host."""
import asyncio
from pathlib import Path

from redpanda.execution.command import CapturedOutput, CommandResult, CommandStartError, ShellNotFoundError
from redpanda.execution.processes import command_identity
from redpanda.assistant.builtin_tools import current_command_id


def execution_result(data: dict, *, background: bool = True, interrupted: bool = False) -> CommandResult:
    reference = {key: data[key] for key in ("command_id", "pid", "cwd", "started_at")}
    return CommandResult(
        exit_code=data["exit_code"], stdout=CapturedOutput(**data["stdout"]),
        stderr=CapturedOutput(**data["stderr"]), duration_ms=data["duration_ms"], timed_out=False,
        interrupted=interrupted and data["state"] == "stopped",
        background=reference if background and data["backgrounded"] else None,
    )


class WorkerCommandPort:
    def __init__(self, request, session_id: str):
        self.request, self.session_id = request, session_id

    async def run(self, command: str, cwd: Path, wait_seconds: int, *, interrupt=None) -> CommandResult:
        initiating_id = current_command_id()
        identity = command_identity(self.session_id, initiating_id)
        request = asyncio.create_task(self.request("command_execute", self.session_id, {
            "command_id": identity, "initiating_id": initiating_id,
            "command": command, "cwd": str(cwd), "wait_seconds": wait_seconds,
        }))
        signal = asyncio.create_task(interrupt.wait()) if interrupt is not None else None
        try:
            if signal is not None:
                done, _ = await asyncio.wait((request, signal), return_when=asyncio.FIRST_COMPLETED)
                if request not in done:
                    # Host serializes this with spawn, so cancelling during launch cannot miss it.
                    await self.request("command_interrupt", self.session_id, {"command_id": identity})
            response = await request
        finally:
            if signal is not None:
                signal.cancel()
                await asyncio.gather(signal, return_exceptions=True)
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        if not response["ok"]:
            if response["code"] == "SHELL_NOT_FOUND":
                raise ShellNotFoundError(response["shell"], response["executable"])
            if response["code"] == "COMMAND_START_FAILED":
                raise CommandStartError(response["error"])
            raise ValueError("Invalid command execution response")
        return execution_result(response["command"], interrupted=signal is not None and signal.done()
                                and not signal.cancelled())

    async def read(self, command_id: str, stdout_offset: int, stderr_offset: int) -> dict | None:
        return await self.request("command_read", self.session_id, {
            "command_id": command_id, "stdout_offset": stdout_offset, "stderr_offset": stderr_offset,
        })

    async def stop(self, command_id: str) -> dict | None:
        return await self.request("command_stop", self.session_id, {"command_id": command_id})

    async def list(self) -> list[dict]:
        return await self.request("command_list", self.session_id, {})
