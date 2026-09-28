from __future__ import annotations

import unicodedata
from typing import Any

from pydantic import BaseModel, Field

from helperme.sandbox.api import (
    EnvironmentBinding,
    environment_error,
)
from helperme.sandbox.workspace import EnvironmentInputError, WorkspacePathResolver
from helperme.tools.spec import PydanticParameters, ToolSpec


APPLY_PATCH_DESCRIPTION = """
用途：在工作区内对单个文本文件执行一次精确且唯一的局部替换。
何时使用：已通过 read_file 或 grep 取得真实原文、只需修改一个明确位置时使用；新建或整体覆盖用 write_file，所有相同文本都要替换时用 replace_all。
关键限制：相对 path 从工作区开始，绝对 path 按本机路径规则；old_block 必须来自最新文件原文并且唯一匹配。
失败/截断后：OLD_BLOCK_NOT_FOUND 后重新 read_file；OLD_BLOCK_NOT_UNIQUE 后扩大上下文；FUZZY_MATCH_ONLY 时使用返回的 original_block 明确重试；本工具结果不截断。
""".strip()

REPLACE_ALL_DESCRIPTION = """
用途：在工作区内把单个文本文件中所有精确匹配的 old_block 批量替换为 new_block。
何时使用：明确希望统一术语、名称或固定字符串的全部出现位置时使用；只改一个位置用 apply_patch，不确定影响范围时先用 grep。
关键限制：相对 path 从工作区开始，绝对 path 按本机路径规则；会修改全部精确匹配，且不支持模糊匹配。
失败/截断后：OLD_BLOCK_NOT_FOUND 后用 grep/read_file 获取最新原文和数量，再决定是否重试；结果不截断，成功后用 get_changes 核对实际改动。
""".strip()


def normalize_for_match(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFKC", text)


class ApplyPatchInput(BaseModel):
    path: str = Field(description="要修改的文本文件路径；相对路径从工作区开始")
    old_block: str = Field(description="必须来自文件原文的精确文本块，且只能匹配一个位置")
    new_block: str = Field(description="替换后的文本块")


class ReplaceAllInput(BaseModel):
    path: str = Field(description="要修改的文本文件路径；相对路径从工作区开始")
    old_block: str = Field(description="要被全文替换的精确文本块")
    new_block: str = Field(description="替换后的文本块")


def _existing_file(resolver: WorkspacePathResolver, path: str):
    resolved = resolver.resolve(path, access="write")
    native = resolved.native_path
    if not native.exists():
        return None, {
            "ok": False,
            "code": "NOT_FOUND",
            "error": f"路径不存在: {path}",
            **resolved.result_fields(),
        }
    if not native.is_file():
        return None, {
            "ok": False,
            "code": "NOT_A_FILE",
            "error": f"不是文件: {path}",
            **resolved.result_fields(),
        }
    return resolved, None


def create_file_write_specs(binding: EnvironmentBinding) -> list[ToolSpec]:
    resolver = binding.resolver

    async def apply_patch(raw: ApplyPatchInput) -> dict[str, Any]:
        try:
            resolved_path, err = _existing_file(resolver, raw.path)
        except EnvironmentInputError as exc:
            return environment_error(exc)
        if err:
            return err
        assert resolved_path is not None
        path = resolved_path.native_path
        relative_path = resolved_path.workspace_membership.display_path
        if not raw.old_block:
            return {
                "ok": False,
                "code": "OLD_BLOCK_EMPTY",
                "path": relative_path,
                **resolved_path.result_fields(),
            }

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "code": "NOT_A_TEXT_FILE",
                "error": f"无法以文本读取: {relative_path}",
                **resolved_path.result_fields(),
            }
        except OSError as exc:
            return _file_io_error(resolved_path, relative_path, exc)

        count = content.count(raw.old_block)
        if count == 1:
            try:
                path.write_text(
                    content.replace(raw.old_block, raw.new_block, 1),
                    encoding="utf-8",
                )
            except OSError as exc:
                return _file_io_error(resolved_path, relative_path, exc)
            return {"ok": True, "code": "PATCH_APPLIED", "path": relative_path, **resolved_path.result_fields(), "replacements": 1}
        if count > 1:
            return {
                "ok": False,
                "code": "OLD_BLOCK_NOT_UNIQUE",
                "matches": count,
                "path": relative_path,
                **resolved_path.result_fields(),
            }

        old_lines = raw.old_block.splitlines(keepends=True)
        window_size = len(old_lines)
        norm_old = normalize_for_match(raw.old_block)
        content_lines = content.splitlines(keepends=True)
        candidates = []
        for index in range(0, len(content_lines) - window_size + 1):
            original_block = "".join(content_lines[index:index + window_size])
            if normalize_for_match(original_block) == norm_old:
                candidates.append({"original_block": original_block, "path": relative_path})
        if len(candidates) == 1:
            return {
                "ok": False,
                "code": "FUZZY_MATCH_ONLY",
                "hint": "old_block 未精确匹配，但找到一个候选。请用 original_block 重试。",
                "candidate": candidates[0],
                "path": relative_path,
                **resolved_path.result_fields(),
            }
        if len(candidates) > 1:
            return {
                "ok": False,
                "code": "FUZZY_MATCH_NOT_UNIQUE",
                "matches": len(candidates),
                "candidates": candidates[:3],
                "path": relative_path,
                **resolved_path.result_fields(),
            }
        return {
            "ok": False,
            "code": "OLD_BLOCK_NOT_FOUND",
            "path": relative_path,
            **resolved_path.result_fields(),
        }

    async def replace_all(raw: ReplaceAllInput) -> dict[str, Any]:
        try:
            resolved_path, err = _existing_file(resolver, raw.path)
        except EnvironmentInputError as exc:
            return environment_error(exc)
        if err:
            return err
        assert resolved_path is not None
        path = resolved_path.native_path
        relative_path = resolved_path.workspace_membership.display_path
        if not raw.old_block:
            return {
                "ok": False,
                "code": "OLD_BLOCK_EMPTY",
                "path": relative_path,
                **resolved_path.result_fields(),
            }
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "code": "NOT_A_TEXT_FILE",
                "error": f"无法以文本读取: {relative_path}",
                **resolved_path.result_fields(),
            }
        except OSError as exc:
            return _file_io_error(resolved_path, relative_path, exc)
        count = content.count(raw.old_block)
        if count == 0:
            return {
                "ok": False,
                "code": "OLD_BLOCK_NOT_FOUND",
                "path": relative_path,
                **resolved_path.result_fields(),
                "replacements": 0,
            }
        try:
            path.write_text(
                content.replace(raw.old_block, raw.new_block),
                encoding="utf-8",
            )
        except OSError as exc:
            return _file_io_error(resolved_path, relative_path, exc)
        return {"ok": True, "code": "REPLACE_ALL_APPLIED", "path": relative_path, **resolved_path.result_fields(), "replacements": count}

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
        "error": f"文件读写失败: {type(exc).__name__}: {exc}",
        "path": relative_path,
        **resolved_path.result_fields(),
    }
