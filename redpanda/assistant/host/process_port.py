"""Session-bound access to processes owned by the Host."""
from pathlib import Path

from redpanda.execution.command import CommandStartError, ShellNotFoundError


class WorkerProcessPort:
    def __init__(self, request, session_id: str):
        self.request = request
        self.session_id = session_id

    async def start(self, command: str, cwd: Path) -> dict:
        result = await self.request("process_start", self.session_id, {"command": command, "cwd": str(cwd)})
        if result["ok"]:
            return result["process"]
        if result["code"] == "SHELL_NOT_FOUND":
            raise ShellNotFoundError(result["shell"], result["executable"])
        if result["code"] == "COMMAND_START_FAILED":
            raise CommandStartError(result["error"])
        raise ValueError("Invalid process start response")

    async def read(self, process_id: str, stdout_offset: int, stderr_offset: int) -> dict | None:
        return await self.request("process_read", self.session_id, {
            "process_id": process_id, "stdout_offset": stdout_offset, "stderr_offset": stderr_offset,
        })

    async def stop(self, process_id: str) -> dict | None:
        return await self.request("process_stop", self.session_id, {"process_id": process_id})

    async def list(self) -> list[dict]:
        return await self.request("process_list", self.session_id, {})
