from __future__ import annotations

from typing import Any

from acp.schema import (
    ContentToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)


_TOOL_KINDS = {
    "read_file": "read",
    "read_artifact": "read",
    "read_image": "read",
    "read_skill_resource": "read",
    "read_compact_source": "read",
    "write_file": "edit",
    "apply_patch": "edit",
    "replace_all": "edit",
    "execute_command": "execute",
    "grep": "search",
    "glob": "search",
    "get_changes": "search",
}


def tool_start(command_id: str, name: str, arguments: Any) -> ToolCallStart:
    return ToolCallStart(
        session_update="tool_call",
        tool_call_id=command_id,
        title=name,
        kind=_TOOL_KINDS.get(name, "other"),
        status="in_progress",
        raw_input=_jsonable(arguments),
    )


def tool_finish(command_id: str, payload: Any, *, failed: bool) -> ToolCallProgress:
    text = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if type(error) is str and error:
            text = error
    return ToolCallProgress(
        session_update="tool_call_update",
        tool_call_id=command_id,
        status="failed" if failed else "completed",
        raw_output=_jsonable(payload),
        content=(
            None
            if text is None
            else [
                ContentToolCallContent(
                    type="content",
                    content=TextContentBlock(type="text", text=text),
                )
            ]
        ),
    )


def tool_failed_from_result(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("ok") is False


def _jsonable(value: Any) -> Any:
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if type(value) is dict:
        return {str(key): _jsonable(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_jsonable(item) for item in value]
    return str(value)
