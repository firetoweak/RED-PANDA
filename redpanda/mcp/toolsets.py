"""MCP Provider 侧 Toolset 目录类型。Runtime 不认识 Toolset。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class ToolsetLoadError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        data: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.data = dict({} if data is None else data)


@dataclass(frozen=True, slots=True)
class ToolsetDescriptor:
    id: str
    description: str
    revision: int = 1
