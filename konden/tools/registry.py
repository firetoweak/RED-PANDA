from __future__ import annotations

from typing import Any

from konden.tools.spec import ToolSpec


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
