from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from redpanda.tools.spec import (
    EmptyInput,
    ToolSpec,
    pydantic_tool_spec,
)


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if type(spec) is not ToolSpec:
            raise TypeError("registry entry must be ToolSpec")
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool name: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def get_tools(self) -> list[dict[str, Any]]:
        return [spec.to_openai_tool() for spec in self._specs.values()]

    def clone(self) -> "ToolRegistry":
        registry = ToolRegistry()
        registry._specs = self._specs.copy()
        return registry

    def select(self, names: set[str]) -> "ToolRegistry":
        registry = ToolRegistry()
        registry._specs = {
            name: spec
            for name, spec in self._specs.items()
            if name in names
        }
        return registry


BUILTIN_TOOL_REGISTRY = ToolRegistry()


def register_tool(description: str, input_model: type[BaseModel] = EmptyInput):
    """装饰器：注册工具，自动生成 schema 和 handler 映射。"""

    def decorator(
        fn: Callable[[BaseModel], Awaitable[dict[str, Any]]],
    ) -> Callable[[BaseModel], Awaitable[dict[str, Any]]]:
        spec = pydantic_tool_spec(
            name=fn.__name__,
            description=description,
            input_model=input_model,
            handler=fn,
        )
        BUILTIN_TOOL_REGISTRY.register(spec)
        return fn

    return decorator
