from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .command import CommandExecutor


class ProcessControl(Protocol):
    async def start(self, command: str, cwd: Path) -> dict: ...
    async def read(self, process_id: str, stdout_offset: int, stderr_offset: int) -> dict | None: ...
    async def stop(self, process_id: str) -> dict | None: ...
    async def list(self) -> list[dict]: ...


@dataclass(frozen=True)
class CommandEnvironment:
    command_executor: CommandExecutor
    shell_name: str
    shell_path: str
    host_command_executor: CommandExecutor

    def __post_init__(self) -> None:
        if not self.shell_name.strip() or not self.shell_path.strip():
            raise ValueError("Shell name and path must be nonempty")
