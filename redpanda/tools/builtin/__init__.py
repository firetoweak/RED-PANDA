from __future__ import annotations

from redpanda.filesystem import FileAccess
from redpanda.execution.api import CommandEnvironment
from redpanda.tools.builtin.command_execution import create_command_execution_spec, command_result
from redpanda.tools.builtin.command_interrupts import (
    CommandInterrupts,
    current_command_id,
    LiveCommand,
    run_interruptible,
)
from redpanda.tools.builtin.file_manage import create_file_manage_specs
from redpanda.tools.builtin.file_read import create_file_read_specs
from redpanda.tools.builtin.file_write import create_file_write_specs
from redpanda.tools.builtin.subagent_workspace import create_subagent_workspace_specs
from redpanda.tools.spec import ToolSpec
from redpanda.tools.builtin.background_commands import create_command_specs

__all__ = [
    "CommandInterrupts",
    "command_result",
    "current_command_id",
    "LiveCommand",
    "create_command_specs",
    "create_subagent_workspace_specs",
    "create_workspace_tool_specs",
    "run_interruptible",
]

def create_workspace_tool_specs(
    binding: FileAccess,
    commands: CommandEnvironment | None,
    interrupts: CommandInterrupts | None = None,
) -> list[ToolSpec]:
    return [
        *create_file_read_specs(binding),
        *create_file_write_specs(binding),
        *create_file_manage_specs(binding),
        *([create_command_execution_spec(
            binding, commands,
            interrupt_source=interrupts.current if interrupts is not None else lambda: None,
        )] if commands is not None else []),
    ]
