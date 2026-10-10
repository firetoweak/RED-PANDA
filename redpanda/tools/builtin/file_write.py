from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from redpanda.sandbox.api import (
    EnvironmentBinding,
    environment_error,
)
from redpanda.sandbox.workspace import EnvironmentInputError
from redpanda.sandbox.files import replace_text
from redpanda.sandbox.api import file_error_message
from redpanda.tools.spec import PydanticParameters, ToolSpec


APPLY_PATCH_DESCRIPTION = """
用途：在工作区内对单个文本文件执行一次精确且唯一的局部替换。
何时使用：已通过 read_file 或 grep 取得真实原文、只需修改一个明确位置时使用；新建或整体覆盖用 write_file，所有相同文本都要替换时用 replace_all。
关键限制：相对 path 从工作区开始，绝对 path 按本机路径规则；old_block 必须来自最新文件原文并且唯一匹配；保留替换范围之外的原文和换行。
失败/截断后：OLD_BLOCK_NOT_FOUND 后重新 read_file；OLD_BLOCK_NOT_UNIQUE 后扩大上下文；FUZZY_MATCH_ONLY 时使用返回的 original_block 明确重试；本工具结果不截断。
""".strip()

REPLACE_ALL_DESCRIPTION = """
用途：在工作区内把单个文本文件中所有精确匹配的 old_block 批量替换为 new_block。
何时使用：明确希望统一术语、名称或固定字符串的全部出现位置时使用；只改一个位置用 apply_patch，不确定影响范围时先用 grep。
关键限制：相对 path 从工作区开始，绝对 path 按本机路径规则；会修改全部精确匹配，且不支持模糊匹配。
失败/截断后：OLD_BLOCK_NOT_FOUND 后用 grep/read_file 获取最新原文和数量，再决定是否重试；结果不截断，成功后用 get_changes 核对实际改动。
""".strip()


class ApplyPatchInput(BaseModel):
    path: str = Field(description="要修改的文本文件路径；相对路径从工作区开始")
    old_block: str = Field(description="必须来自文件原文的精确文本块，且只能匹配一个位置")
    new_block: str = Field(description="替换后的文本块")


class ReplaceAllInput(BaseModel):
    path: str = Field(description="要修改的文本文件路径；相对路径从工作区开始")
    old_block: str = Field(description="要被全文替换的精确文本块")
    new_block: str = Field(description="替换后的文本块")


def create_file_write_specs(binding: EnvironmentBinding) -> list[ToolSpec]:
    resolver = binding.resolver

    async def replace(raw, *, all_matches):
        try:
            resolved = resolver.resolve(raw.path, access="write")
        except EnvironmentInputError as exc:
            return environment_error(exc)
        except OSError as exc:
            return {"ok": False, "code": "FILE_IO_FAILED", "error": file_error_message(exc), "path": raw.path}
        try:
            result = await replace_text(resolved.native_path, raw.old_block, raw.new_block, all_matches=all_matches)
        except UnicodeDecodeError:
            return {"ok": False, "code": "NOT_A_TEXT_FILE", "path": resolved.workspace_membership.display_path, **resolved.result_fields()}
        except OSError as exc:
            return _file_io_error(resolved, resolved.workspace_membership.display_path, exc)
        path = resolved.workspace_membership.display_path
        for candidate in result.get("candidates", []):
            candidate["path"] = path
        if result["code"] == "FUZZY_MATCH_ONLY":
            result["candidate"] = result.pop("candidates")[0]
            result["hint"] = "old_block 未精确匹配；请使用返回的 original_block 重试。"
        return {**result, "path": path, **resolved.result_fields()}

    async def apply_patch(raw: ApplyPatchInput):
        return await replace(raw, all_matches=False)

    async def replace_all(raw: ReplaceAllInput):
        return await replace(raw, all_matches=True)

    return [
        ToolSpec(
            name="apply_patch",
            description=APPLY_PATCH_DESCRIPTION,
            parameters=PydanticParameters(ApplyPatchInput),
            handler=apply_patch,
            requires_authorization=True,
        ),
        ToolSpec(
            name="replace_all",
            description=REPLACE_ALL_DESCRIPTION,
            parameters=PydanticParameters(ReplaceAllInput),
            handler=replace_all,
            requires_authorization=True,
        ),
    ]


def _file_io_error(resolved_path, relative_path: str, exc: OSError):
    return {
        "ok": False,
        "code": "FILE_IO_FAILED",
        "error": file_error_message(exc),
        "path": relative_path,
        **resolved_path.result_fields(),
    }
