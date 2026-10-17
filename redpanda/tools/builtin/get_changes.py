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
用途：修改文件或执行命令后核对实际效果，发现漏改、误改，并在最终汇报前确认当前仍保留的成果。
范围选择：scope 默认 session，只核对当前会话涉及的文件；分支会话包含继承对话中的修改。只有需要核对整个工作区的助手修改时才用 workspace，其中包含所有会话，不能全部作为当前会话的成果。两种范围都不负责发现助手从未涉及的文件变化。
返回内容：返回修改前后文本（edits）和文件属性变化（properties）。session 范围中，origin=agent 表示当前会话仍保留的修改，external 表示当前会话之外的变化；workspace 范围中，agent 表示该工作区所有会话的助手修改，external 表示这些助手修改之外的变化。mixed 表示两者混合，unknown 表示无法判断，unchanged 表示可比较部分已回到原值。每段文本有独立的 origin，同一段为 mixed 时不能把整段算作助手成果。byte_offset 是 after 在当前文件中的起始字节位置，从 0 开始。
关键限制：原值是所选范围首次修改相关位置之前的值；被覆盖、撤销或恢复原值的内容不算现存成果。普通目录也可用，不依赖 Git。
续读：保持 scope 不变。next_offset 非空时用 offset 继续列文件；文件的 next_text_offset 非空时，把 path 指定为该文件并用 text_offset 继续读差异，直接使用返回的续读位置。每段的 text_offset 表明片段从该段哪个字符开始，truncated=true 的片段不能当作完整替换。文件再次修改后从头查询。
失败/截断后：content_complete=false 时只对已展示且来源明确的部分作结论；read_file 可以核对当前内容，不能补出未知原值。查询失败不能宣称已经验证或没有改动。功能正确与否仍需另外测试。
""".strip()


class GetChangesInput(BaseModel):
    path: str = Field(default=".", description="要检查的文件或目录")
    scope: Literal["session", "workspace"] = Field(default="session", description="session 核对当前会话（默认，含分支继承的修改）；workspace 核对工作区所有会话的助手修改")
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
