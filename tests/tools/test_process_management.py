import pytest

from unittest.mock import AsyncMock

from redpanda.filesystem import FileAccess
from redpanda.filesystem.workspace import PermissionBinding, RootBinding, WorkspaceScope, WorkspaceViewSnapshot
from redpanda.tools.builtin.process_management import create_process_specs
from redpanda.tools.spec import ToolArgumentsError


def test_process_output_schema_rejects_negative_cursors_and_extra_fields(tmp_path):
    view = WorkspaceViewSnapshot((RootBinding("project", WorkspaceScope.TASK, tmp_path),))
    binding = FileAccess("local", view, PermissionBinding.read_write(view), tmp_path)
    specs = {spec.name: spec for spec in create_process_specs(binding, AsyncMock())}
    with pytest.raises(ToolArgumentsError):
        specs["read_process"].parameters.validate({"process_id": "x", "stdout_offset": -1})
    with pytest.raises(ToolArgumentsError):
        specs["start_process"].parameters.validate({"command": "x", "file_access": "managed"})
    with pytest.raises(ToolArgumentsError):
        specs["start_process"].parameters.validate({"command": "echo x\x00; echo y"})
