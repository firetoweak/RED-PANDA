from __future__ import annotations

from dataclasses import dataclass

from .command import CommandExecutor


@dataclass(frozen=True)
class CommandEnvironment:
    command_executor: CommandExecutor
    shell_name: str
    shell_path: str

    def __post_init__(self) -> None:
        if not self.shell_name.strip() or not self.shell_path.strip():
            raise ValueError("Shell name and path must be nonempty")
