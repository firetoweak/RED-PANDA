from __future__ import annotations

import unittest

from pydantic import BaseModel

from konden.assistant.builtin_tools import BuiltinToolRunner
from konden.runtime import InvokeTool
from konden.tools.builtin import CommandInterrupts
from konden.tools.executor import ToolsExecutor
from konden.tools.registry import ToolRegistry
from konden.tools.spec import pydantic_tool_spec


class _NestedItem(BaseModel):
    value: int


class _NestedInput(BaseModel):
    items: list[_NestedItem]


class BuiltinToolRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_executes_frozen_nested_arguments_without_json_round_trip(self):
        async def handler(value: _NestedInput):
            return {"ok": True, "code": "OK", "data": value.model_dump()}

        registry = ToolRegistry()
        registry.register(
            pydantic_tool_spec(
                name="nested",
                description="nested arguments",
                input_model=_NestedInput,
                handler=handler,
            )
        )
        runner = BuiltinToolRunner(
            tuple(registry.get_tools()),
            CommandInterrupts(),
            ToolsExecutor(registry),
        )
        arguments = InvokeTool(
            "nested",
            (("items", [{"value": 1}, {"value": 2}]),),
        ).argument_dict()

        result = await runner.execute("nested", arguments)

        self.assertEqual(
            result,
            {
                "ok": True,
                "code": "OK",
                "data": {"items": [{"value": 1}, {"value": 2}]},
                "error": None,
                "hint": None,
            },
        )
