from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from helperme.sandbox.api import (
    EnvironmentBinding,
    environment_error,
)
from helperme.sandbox.workspace import EnvironmentInputError
from helperme.tools.spec import PydanticParameters, ToolSpec


WRITE_FILE_DESCRIPTION = """
用途：在工作区内创建文本文件，或在明确允许时整体覆盖已有文件；父目录不存在时自动创建。
何时使用：新建文章、代码、计划等完整文件，或目标明确要求重写整个文件时使用；局部修改用 apply_patch，批量替换用 replace_all，查找和读取用 glob/grep/read_file。
关键限制：相对 path 从工作区开始，绝对 path 按本机路径规则；overwrite=false 时绝不覆盖已有文件。
失败/截断后：结果不截断；FILE_EXISTS 后不要擅自改为 overwrite=true，应先读现有内容并改用局部编辑，目标明确要求整体重写时才覆盖；IS_A_DIR 时补充文件名；路径错误时修正 path 后重试。
""".strip()


class WriteFileInput(BaseModel):
    path: str = Field(description="要写入的文件路径；相对路径从工作区开始")
    content: str = Field(description="要写入的完整文本内容；显式传入空字符串表示创建空文件")
    overwrite: bool = Field(default=False, description="文件已存在时是否整体覆盖；false 拒绝覆盖，true 完整覆盖")


def create_file_manage_specs(binding: EnvironmentBinding) -> list[ToolSpec]:
    resolver = binding.resolver

    async def write_file(raw: WriteFileInput) -> dict[str, Any]:
        try:
            resolved_path = resolver.resolve(raw.path, access="write")
        except EnvironmentInputError as exc:
            return environment_error(exc)

        path = resolved_path.native_path
        relative_path = resolved_path.workspace_membership.display_path
        try:
            if path.exists() and path.is_dir():
                return {
                    "ok": False,
                    "code": "IS_A_DIR",
                    "path": relative_path,
                    **resolved_path.result_fields(),
                }
            existed = path.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            if raw.overwrite:
                path.write_text(raw.content, encoding="utf-8")
            else:
                try:
                    with path.open("x", encoding="utf-8") as handle:
                        handle.write(raw.content)
                except FileExistsError:
                    return {
                        "ok": False,
                        "code": "FILE_EXISTS",
                        "path": relative_path,
                        **resolved_path.result_fields(),
                    }
        except OSError as exc:
            return {
                "ok": False,
                "code": "FILE_WRITE_FAILED",
                "error": f"写入文件失败: {type(exc).__name__}: {exc}",
                "path": relative_path,
                **resolved_path.result_fields(),
            }
        return {
            "ok": True,
            "code": "FILE_OVERWRITTEN" if existed else "FILE_CREATED",
            "path": relative_path,
            **resolved_path.result_fields(),
        }

    return [
        ToolSpec(
            name="write_file",
            description=WRITE_FILE_DESCRIPTION,
            parameters=PydanticParameters(WriteFileInput),
            handler=write_file,
            requires_authorization=True,
        )
    ]
