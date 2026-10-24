from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .command import CommandExecutor


class CommandControl(CommandExecutor, Protocol):
    async def read(self, command_id: str, stdout_offset: int, stderr_offset: int) -> dict | None: ...
    async def stop(self, command_id: str) -> dict | None: ...
    async def list(self) -> list[dict]: ...


@dataclass(frozen=True)
class CommandEnvironment:
    command_executor: CommandExecutor
    shell_name: str
    shell_path: str
    background_supported: bool = True

    def __post_init__(self) -> None:
        if not self.shell_name.strip() or not self.shell_path.strip():
            raise ValueError("Shell name and path must be nonempty")
