import asyncio
from unittest.mock import AsyncMock

import pytest

from redpanda.execution.api import CommandEnvironment
from redpanda.execution.command import CapturedOutput, CommandResult
from redpanda.filesystem import FileAccess
from redpanda.filesystem.workspace import (
    FilesystemPermission, PermissionBinding, RootBinding, WorkspaceScope, WorkspaceViewSnapshot,
)
from redpanda.tools.builtin.command_execution import ExecuteCommandInput, create_command_execution_spec
from redpanda.tools.builtin.exec_policy import command_requires_authorization
from redpanda.tools.builtin.process_management import StartProcessInput, create_process_specs


def binding_at(root, permission=FilesystemPermission.READ_WRITE):
    class MissingFiles:
        def native_path(self, path):
            raise RuntimeError("File service unavailable")
    view = WorkspaceViewSnapshot((RootBinding("project", WorkspaceScope.TASK, root),))
    return FileAccess("local", view, PermissionBinding((("project", permission),)), root, MissingFiles())


def test_host_uses_real_cwd_without_projection_or_managed_fallback(tmp_path):
    async def scenario():
        output = CapturedOutput("HOST", 4, False, 0)
        host = AsyncMock()
        host.run.return_value = CommandResult(0, output, output, 1, False)
        managed = AsyncMock()
        spec = create_command_execution_spec(binding_at(tmp_path), CommandEnvironment(managed, "test", "test", host))
        result = await spec.handler(ExecuteCommandInput(command="x", file_access="host"))
        assert result["data"]["file_access"] == "host"
        host.run.assert_awaited_once_with("x", tmp_path, 60)
        with pytest.raises(RuntimeError, match="File service unavailable"):
            await spec.handler(ExecuteCommandInput(command="x"))
        managed.run.assert_not_awaited()
        assert host.run.await_count == 1
    asyncio.run(scenario())


def test_host_keeps_path_permissions_and_write_authorization(tmp_path):
    async def scenario():
        runner = AsyncMock()
        binding = binding_at(tmp_path, FilesystemPermission.READ_ONLY)
        spec = create_command_execution_spec(binding, CommandEnvironment(runner, "test", "test", runner))
        denied = await spec.handler(ExecuteCommandInput(command="x", file_access="host"))
        outside = await spec.handler(ExecuteCommandInput(command="x", cwd=str(tmp_path.parent), file_access="host"))
        assert denied["ok"] is False and outside["ok"] is False
        processes = AsyncMock()
        start = create_process_specs(binding, processes)[0]
        assert start.requires_authorization is True
        assert (await start.handler(StartProcessInput(command="x")))["ok"] is False
        runner.run.assert_not_awaited()
        processes.start.assert_not_awaited()
    asyncio.run(scenario())
    assert command_requires_authorization({"command": "echo x", "file_access": "host"})
    assert not command_requires_authorization({"command": "echo x", "file_access": "managed"})
    assert command_requires_authorization({"command": "Remove-Item x", "file_access": "host", "workspace_effect": "read_only"})


def test_process_output_schema_rejects_negative_cursors_and_extra_fields(tmp_path):
    from redpanda.tools.spec import ToolArgumentsError
    specs = {spec.name: spec for spec in create_process_specs(binding_at(tmp_path), AsyncMock())}
    with pytest.raises(ToolArgumentsError):
        specs["read_process"].parameters.validate({"process_id": "x", "stdout_offset": -1})
    with pytest.raises(ToolArgumentsError):
        specs["start_process"].parameters.validate({"command": "x", "file_access": "managed"})
    with pytest.raises(ToolArgumentsError):
        specs["start_process"].parameters.validate({"command": "echo x\x00; echo y"})
