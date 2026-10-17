from __future__ import annotations

from redpanda.filesystem import FileAccess
from redpanda.execution.api import CommandEnvironment
from redpanda.tools.builtin.command_execution import create_command_execution_spec
from redpanda.tools.builtin.command_interrupts import (
    CommandInterrupts,
    LiveCommand,
    run_interruptible,
)
from redpanda.tools.builtin.file_manage import create_file_manage_specs
from redpanda.tools.builtin.file_read import create_file_read_specs
from redpanda.tools.builtin.file_write import create_file_write_specs
from redpanda.tools.builtin.get_changes import create_get_changes_specs
from redpanda.tools.builtin.workspace_restore import create_workspace_restore_spec
from redpanda.tools.builtin.subagent_workspace import create_subagent_workspace_specs
from redpanda.tools.spec import ToolSpec
from redpanda.tools.builtin.process_management import create_process_specs

__all__ = [
    "CommandInterrupts",
    "LiveCommand",
    "READ_ONLY_WORKSPACE_TOOLS",
    "create_process_specs",
    "create_subagent_workspace_specs",
    "create_workspace_restore_spec",
    "create_workspace_tool_specs",
    "run_interruptible",
]

READ_ONLY_WORKSPACE_TOOLS = frozenset({"glob", "grep", "read_file", "get_changes"})


def create_workspace_tool_specs(
    binding: FileAccess,
    commands: CommandEnvironment,
    interrupts: CommandInterrupts | None = None,
    *,
    session_operations,
) -> list[ToolSpec]:
    return [
        *create_file_read_specs(binding),
        *create_file_write_specs(binding),
        *create_file_manage_specs(binding),
        *create_get_changes_specs(binding, session_operations),
        create_command_execution_spec(binding, commands)
        if interrupts is None
        else create_command_execution_spec(binding, commands, interrupts.current),
    ]
