from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from redpanda.filesystem import FileAccess
from redpanda.tools.results import environment_error, file_error_message
from redpanda.filesystem import read_changes, ChangesReadConflict
from redpanda.filesystem.workspace import EnvironmentInputError, WorkspaceScope
from redpanda.tools.spec import PydanticParameters, ToolSpec


GET_CHANGES_DESCRIPTION = """
用途：核对文件的实际修改（修改前后文本 edits、属性 properties），发现漏改、误改，并在汇报前确认成果仍在。只覆盖文件工具和 managed 命令改过的文件，不依赖 Git。
origin：agent=所选范围内助手仍保留的修改，external=范围之外的变化，mixed=两者混合（不能整段算作自己的成果），unknown=无法判断，unchanged=已回到原值。原值是该范围首次修改前的值。byte_offset 是 after 在当前文件中的起始字节位置。
续读：保持 scope 不变；next_offset 非空时用 offset 继续列文件；某文件 next_text_offset 非空时，path 指向该文件并用 text_offset 继续。truncated=true 的片段不完整。文件再次修改后从头查询。
content_complete=false 时只对已展示且来源明确的部分下结论；read_file 只能看当前内容，补不出原值。
""".strip()


class GetChangesInput(BaseModel):
    path: str = Field(default=".", description="要检查的文件或目录")
    scope: Literal["session", "workspace"] = Field(default="session", description="session（默认）：当前会话的修改，含分出会话时继承的修改；workspace：该工作区所有会话的助手修改，不能都算作当前会话成果")
    offset: int = Field(default=0, ge=0, description="文件列表续读位置；首次为 0，续查使用 next_offset")
    text_offset: int = Field(default=0, ge=0, description="单文件差异的续读位置；使用该文件返回的 next_text_offset")


def create_get_changes_specs(binding: FileAccess, session_operations: Callable[[], Awaitable[tuple[str, ...]]]) -> list[ToolSpec]:
    resolver = binding.resolver

    async def get_changes(raw: GetChangesInput) -> dict[str, Any]:
        try:
            resolved = resolver.resolve(raw.path)
            filesystem = (binding.filesystem
                          if resolved.workspace_membership.scope is WorkspaceScope.TASK else None)
            logical = (filesystem.logical_path(resolved.native_path)
                       if filesystem is not None else resolved.native_path)
            operations = await session_operations() if raw.scope == "session" and filesystem is not None else None
            result = await read_changes(logical, filesystem=filesystem, operations=operations,
                                        offset=raw.offset, text_offset=raw.text_offset)
            return result
        except EnvironmentInputError as exc:
            return environment_error(exc)
        except OSError as exc:
            return {"ok": False, "code": "CHANGES_READ_FAILED",
                    "error": str(exc) if isinstance(exc, ChangesReadConflict) else file_error_message(exc)}

    return [ToolSpec(name="get_changes", description=GET_CHANGES_DESCRIPTION,
                     parameters=PydanticParameters(GetChangesInput), handler=get_changes)]
