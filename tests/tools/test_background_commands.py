import pytest
from unittest.mock import AsyncMock
from konden.tools.builtin.background_commands import create_command_specs
from konden.tools.spec import ToolArgumentsError


def test_command_output_schema_rejects_negative_cursors_and_extra_fields():
    specs = {spec.name: spec for spec in create_command_specs(AsyncMock())}
    with pytest.raises(ToolArgumentsError):
        specs["read_command"].parameters.validate({"command_id": "x", "stdout_offset": -1})
    with pytest.raises(ToolArgumentsError):
        specs["stop_command"].parameters.validate({"command_id": "x", "unexpected": True})
