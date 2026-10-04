from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
from typing import Any, Literal

from pydantic import BaseModel, Field

from redpanda.sandbox.api import (
    EnvironmentBinding,
    environment_error,
)
from redpanda.sandbox.workspace import (
    EnvironmentInputError,
    InvalidEnvironmentPath,
    ResolvedEnvironmentPath,
    WorkspacePathResolver,
)
from redpanda.tools.spec import PydanticParameters, ToolSpec


RG_NOT_FOUND_HINT = "本机缺少 ripgrep，glob/grep 暂不可用；已知路径时改用 read_file，并告诉用户需要安装 ripgrep。"

GLOB_DESCRIPTION = """
用途：在工作区内按名称模式查找文件或目录。
何时使用：不知道目标文件位置、需要按扩展名或目录层级定位时使用；搜索文件内容用 grep，读取已知文件用 read_file。
默认范围：使用 rg 默认过滤：跳过隐藏文件/目录（名称以 . 开头，含 .git）以及 gitignore / .ignore / .rgignore 匹配项。需要搜索隐藏文件时设 include_hidden=true，需要搜索 gitignore 匹配项时设 include_ignored=true，两者可同时设置；把 path 直接指到被跳过的目录会进入该目录，进入后上述过滤规则依然生效。无论 include_hidden 还是 include_ignored 为 true 都不进入 .git，除非 path 已在 .git 内。
关键限制：相对 path 从工作区开始，绝对 path 按本机路径规则；pattern 不含 / 时递归匹配文件名，含 / 时匹配相对搜索起点的完整路径，** 匹配零层或任意多层目录；结果只包含工作区内的路径。
失败/截断后：truncated=true 时用返回的 next_offset 作为下次调用的 offset 继续，或缩小 path、pattern、kind、max_depth；hint 会说明本次跳过了哪些过滤；RG_TIMEOUT/RG_NOT_FOUND/RG_FAILED 时不能假定没有匹配。
""".strip()

GREP_DESCRIPTION = """
用途：在工作区内用正则搜索文本，返回每个匹配行的位置和内容。
何时使用：不知道内容出现在哪个文件、修改前需要定位原文时使用；找文件名用 glob，阅读匹配位置的完整上下文用 read_file。
默认范围：使用 rg 默认过滤：跳过隐藏文件/目录（名称以 . 开头，含 .git）以及 gitignore / .ignore / .rgignore 匹配项。需要搜索隐藏文件时设 include_hidden=true，需要搜索 gitignore 匹配项时设 include_ignored=true，两者可同时设置；把 path 直接指到被跳过的文件或目录会搜索该路径，对目录而言进入后上述过滤规则依然生效。无论 include_hidden 还是 include_ignored 为 true 都不进入 .git，除非 path 已在 .git 内。
关键限制：相对 path 从工作区开始，绝对 path 按本机路径规则；query 始终按正则解释，字面匹配需转义正则元字符；一条 hit 表示一行匹配。
失败/截断后：truncated=true 时用返回的 next_offset 作为下次调用的 offset 继续，或缩小 path/query；content_truncated=true 时用 read_file 查看该行；RG_TIMEOUT/RG_NOT_FOUND/RG_FAILED 时不能假定没有匹配。
""".strip()

READ_FILE_DESCRIPTION = """
用途：读取工作区内已知文本文件的行范围，返回内容、行号和分页状态。
何时使用：已经知道文件路径、需要查看 grep 命中的完整上下文或在修改前取得真实 old_block 时使用；不知道路径先用 glob/grep，不要用它读取二进制文件。
关键限制：相对 path 从工作区开始，绝对 path 按本机路径规则；offset 从 1 开始；单次最多 2000 行和 8000 字符。文件体积不是拒绝条件。
失败/截断后：truncated=true 时使用 next_offset 继续；LINE_TOO_LONG 返回有界 preview 但不声称读取成功。
""".strip()


RG_STREAM_LIMIT_BYTES = 20 * 1024 * 1024
MAX_READ_CHARS = 8_000
MAX_GREP_HIT_CHARS = 2_000
MAX_GREP_PAGE_CHARS = 8_000
MAX_GREP_SUBMATCHES = 100
MAX_RG_ERROR_CHARS = 4_000
GREP_TIMEOUT_SECONDS = 30
GREP_SHUTDOWN_TIMEOUT_SECONDS = 5


class GlobInput(BaseModel):
    pattern: str = Field(description="glob 模式；不含 / 时递归匹配文件名，含 / 时匹配相对搜索起点的路径，例如 *.py、tools/*.py")
    path: str = Field(default=".", description="搜索起点；相对路径从工作区开始")
    kind: Literal["file", "dir", "any"] = Field(default="any", description="结果类型：file 仅文件、dir 仅目录、any 两者都要")
    max_depth: int | None = Field(default=None, ge=1, description="相对搜索起点的深度限制；默认不限制，1 表示只看当前层")
    include_hidden: bool = Field(
        default=False,
        description="为 true 时包含隐藏文件/目录（名称以 . 开头）；默认跳过。.git 仍会跳过，除非 path 已在 .git 内",
    )
    include_ignored: bool = Field(
        default=False,
        description="为 true 时包含 gitignore / .ignore / .rgignore 匹配项；默认跳过。把 path 指到被忽略目录也会进入",
    )
    offset: int = Field(default=0, ge=0, description="跳过的匹配结果数量，从 0 开始")
    max_results: int = Field(default=10, ge=1, le=100, description="最多返回的结果数量，范围 1 到 100")


class ReadFileInput(BaseModel):
    path: str = Field(description="要读取的文件路径；相对路径从工作区开始")
    offset: int = Field(default=1, ge=1, description="读取起始行号，从 1 开始")
    limit: int = Field(default=200, ge=1, le=2000, description="最多读取行数，范围 1 到 2000")


class GrepInput(BaseModel):
    query: str = Field(description="正则表达式；字面匹配需转义正则元字符")
    path: str = Field(default=".", description="搜索路径；相对路径从工作区开始")
    include_hidden: bool = Field(
        default=False,
        description="为 true 时搜索隐藏文件/目录（名称以 . 开头）；默认跳过。.git 仍会跳过，除非 path 已在 .git 内",
    )
    include_ignored: bool = Field(
        default=False,
        description="为 true 时搜索 gitignore / .ignore / .rgignore 匹配项；默认跳过。把 path 指到被忽略文件或目录也会搜索",
    )
    offset: int = Field(default=0, ge=0, description="跳过的匹配行数量，从 0 开始")
    max_results: int = Field(default=10, ge=1, le=100, description="最多返回的匹配数量，范围 1 到 100")


def _require_existing(
    resolver: WorkspacePathResolver,
    path: str,
    *,
    expect: Literal["file", "dir", "any"],
) -> tuple[ResolvedEnvironmentPath | None, dict[str, Any] | None]:
    resolved = resolver.resolve(path)
    native = resolved.native_path
    if not native.exists():
        return None, {
            "ok": False,
            "code": "NOT_FOUND",
            "error": f"路径不存在: {path}",
            **resolved.result_fields(),
        }
    if expect == "file" and not native.is_file():
        return None, {
            "ok": False,
            "code": "NOT_A_FILE",
            "error": f"不是文件: {path}",
            **resolved.result_fields(),
        }
    if expect == "dir" and not native.is_dir():
        return None, {
            "ok": False,
            "code": "NOT_A_DIR",
            "error": f"不是目录: {path}",
            **resolved.result_fields(),
        }
    return resolved, None


def _is_within_git_dir(path: Path) -> bool:
    return any(part == ".git" for part in path.parts)


def _rg_scope_args(
    native_path: Path,
    *,
    include_hidden: bool,
    include_ignored: bool,
) -> list[str]:
    args: list[str] = []
    if include_hidden:
        args.append("--hidden")
    if include_ignored:
        args.append("--no-ignore")
    if include_hidden and not _is_within_git_dir(native_path):
        args.extend(["--glob", "!.git/"])
    return args


def _rg_scope_hint(
    native_path: Path,
    *,
    include_hidden: bool,
    include_ignored: bool,
) -> str | None:
    skipped: list[str] = []
    if not include_hidden:
        skipped.append("隐藏文件/目录")
    elif not _is_within_git_dir(native_path):
        skipped.append(".git")
    if not include_ignored:
        skipped.append("gitignore 匹配项")
    if not skipped:
        return None
    knobs: list[str] = []
    if not include_hidden:
        knobs.append("include_hidden=true")
    if not include_ignored:
        knobs.append("include_ignored=true")
    knobs.append("或把 path 指到目标目录")
    return f"已跳过{'、'.join(skipped)}。需要时设 {' / '.join(knobs)}。"


def _matches_glob(path: str, pattern: str) -> bool:
    candidate = PurePosixPath(path)
    if "/" not in pattern:
        return PurePosixPath(candidate.name).match(pattern)
    return candidate.full_match(pattern)


def _glob_relative_entries(
    file_paths: list[Path],
    search_root: Path,
    max_depth: int | None,
) -> list[tuple[str, Literal["file", "dir"]]]:
    files: set[str] = set()
    dirs: set[str] = set()
    root = search_root.resolve()
    for path in file_paths:
        try:
            relative = path.resolve().relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if not parts:
            continue
        if max_depth is None or len(parts) <= max_depth:
            files.add(relative.as_posix())
        for depth in range(1, len(parts)):
            if max_depth is None or depth <= max_depth:
                dirs.add(PurePosixPath(*parts[:depth]).as_posix())
    entries: list[tuple[str, Literal["file", "dir"]]] = [
        *((item, "dir") for item in dirs),
        *((item, "file") for item in files),
    ]
    entries.sort(key=lambda item: item[0])
    return entries


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), GREP_SHUTDOWN_TIMEOUT_SECONDS)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


async def _rg_file_paths(search_root: Path, scope_args: list[str]) -> dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "rg",
            "--files",
            "-0",
            *scope_args,
            "--",
            str(search_root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=RG_STREAM_LIMIT_BYTES,
        )
    except OSError as exc:
        return _filesystem_failure("RG_FAILED", str(search_root), exc)
    timed_out = False
    try:
        try:
            async with asyncio.timeout(GREP_TIMEOUT_SECONDS):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            timed_out = True
            await _stop_process(proc)
            stdout, stderr = b"", b""
    except BaseException:
        cleanup = asyncio.create_task(_stop_process(proc))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
        raise
    if timed_out:
        return {
            "ok": False,
            "code": "RG_TIMEOUT",
            "error": f"rg 搜索超过 {GREP_TIMEOUT_SECONDS} 秒",
        }
    stderr_text = stderr.decode("utf-8", errors="replace").strip()[:MAX_RG_ERROR_CHARS]
    if proc.returncode == 2:
        return {
            "ok": False,
            "code": "RG_FAILED",
            "error": stderr_text or "rg 执行失败",
        }
    paths: list[Path] = []
    for raw in stdout.split(b"\0"):
        if not raw:
            continue
        candidate = Path(os.fsdecode(raw))
        if not candidate.is_absolute():
            candidate = search_root / candidate
        paths.append(candidate)
    return {"ok": True, "paths": paths}


def create_file_read_specs(binding: EnvironmentBinding) -> list[ToolSpec]:
    resolver = binding.resolver

    async def glob(raw: GlobInput) -> dict[str, Any]:
        try:
            resolved_search, err = _require_existing(
                resolver, raw.path, expect="dir"
            )
            if err:
                return err
            assert resolved_search is not None

            pattern = raw.pattern.replace("\\", "/")
            pattern_path = Path(pattern)
            if pattern_path.is_absolute() or pattern_path.drive:
                raise InvalidEnvironmentPath("glob pattern 必须相对搜索起点")
            if ".." in pattern_path.parts:
                raise InvalidEnvironmentPath("glob pattern 不能包含 ..")
        except EnvironmentInputError as exc:
            return environment_error(exc)
        except OSError as exc:
            return _filesystem_failure("GLOB_FAILED", raw.path, exc)

        if not pattern.strip():
            return {
                "ok": False,
                "error": "pattern 不能为空",
                "code": "EMPTY_PATTERN",
                **resolved_search.result_fields(),
            }
        if shutil.which("rg") is None:
            return {"ok": False, "code": "RG_NOT_FOUND", "error": "未找到 rg", "hint": RG_NOT_FOUND_HINT}

        search_root = resolved_search.native_path
        scope_args = _rg_scope_args(
            search_root,
            include_hidden=raw.include_hidden,
            include_ignored=raw.include_ignored,
        )
        listed = await _rg_file_paths(search_root, scope_args)
        if not listed.get("ok"):
            return {
                **listed,
                "path": raw.path,
                **resolved_search.result_fields(),
            }

        selected = [
            item
            for item in _glob_relative_entries(
                listed["paths"],
                search_root,
                raw.max_depth,
            )
            if (raw.kind == "any" or item[1] == raw.kind)
            and _matches_glob(item[0], pattern)
        ]
        page = selected[raw.offset:raw.offset + raw.max_results]
        truncated = len(selected) > raw.offset + raw.max_results
        matches = []
        for relative, candidate_kind in page:
            try:
                resolved_candidate = resolver.resolve(str(search_root / relative))
            except EnvironmentInputError:
                continue
            matches.append({
                **resolved_candidate.result_fields(),
                "path": resolved_candidate.workspace_membership.display_path,
                "kind": candidate_kind,
            })
        return {
            "ok": True,
            "code": "GLOB_COMPLETED",
            "pattern": raw.pattern,
            "path": raw.path,
            **resolved_search.result_fields(),
            "matches": matches,
            "truncated": truncated,
            "next_offset": raw.offset + len(page) if truncated else None,
            "hint": _rg_scope_hint(
                search_root,
                include_hidden=raw.include_hidden,
                include_ignored=raw.include_ignored,
            ),
        }

    async def grep(raw: GrepInput) -> dict[str, Any]:
        if not raw.query.strip():
            return {"ok": False, "code": "EMPTY_QUERY", "error": "query 不能为空"}
        if shutil.which("rg") is None:
            return {"ok": False, "code": "RG_NOT_FOUND", "error": "未找到 rg", "hint": RG_NOT_FOUND_HINT}
        try:
            resolved_path, err = _require_existing(
                resolver, raw.path, expect="any"
            )
        except EnvironmentInputError as exc:
            return environment_error(exc)
        except OSError as exc:
            return _filesystem_failure("RG_FAILED", raw.path, exc)
        if err:
            return err
        assert resolved_path is not None
        path = resolved_path.native_path

        hits = []
        skipped = 0
        truncated = False
        page_chars = 0
        try:
            proc = await asyncio.create_subprocess_exec(
                "rg",
                "--json",
                "--sort",
                "path",
                *_rg_scope_args(
                    path,
                    include_hidden=raw.include_hidden,
                    include_ignored=raw.include_ignored,
                ),
                "--",
                raw.query,
                str(path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=RG_STREAM_LIMIT_BYTES,
            )
        except OSError as exc:
            return _filesystem_failure("RG_FAILED", raw.path, exc)
        assert proc.stdout is not None
        assert proc.stderr is not None

        async def drain_stderr() -> str:
            captured = bytearray()
            while chunk := await proc.stderr.read(4_096):
                remaining = MAX_RG_ERROR_CHARS - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
            return captured.decode("utf-8", errors="replace").strip()

        stderr_task = asyncio.create_task(drain_stderr())

        async def stop_process() -> str:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(
                        proc.wait(),
                        GREP_SHUTDOWN_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
            return await stderr_task

        timed_out = False
        protocol_error: str | None = None
        try:
            try:
                async with asyncio.timeout(GREP_TIMEOUT_SECONDS):
                    while raw_bytes := await proc.stdout.readline():
                        raw_line = raw_bytes.decode("utf-8", errors="replace")
                        if not raw_line.strip():
                            continue
                        try:
                            obj = json.loads(raw_line)
                            if not isinstance(obj, dict):
                                raise TypeError("event must be object")
                            event_type = obj["type"]
                            if event_type != "match":
                                if event_type not in {"begin", "end", "summary"}:
                                    raise ValueError(
                                        f"unknown event type: {event_type}"
                                    )
                                continue
                            data = obj["data"]
                            if not isinstance(data, dict):
                                raise TypeError("match data must be object")
                            lines = data["lines"]
                            path_data = data["path"]
                            submatches = data["submatches"]
                            line_number = data["line_number"]
                            if (
                                not isinstance(lines, dict)
                                or type(lines["text"]) is not str
                                or not isinstance(path_data, dict)
                                or type(path_data["text"]) is not str
                                or type(line_number) is not int
                                or not isinstance(submatches, list)
                                or any(
                                    not isinstance(match, dict)
                                    or type(match["start"]) is not int
                                    or type(match["end"]) is not int
                                    for match in submatches
                                )
                            ):
                                raise TypeError("match payload fields are invalid")
                        except (
                            json.JSONDecodeError,
                            KeyError,
                            TypeError,
                            ValueError,
                        ) as exc:
                            protocol_error = (
                                "rg JSON 协议无效: "
                                f"{type(exc).__name__}: {exc}"
                            )
                            break
                        if event_type != "match":
                            continue
                        if skipped < raw.offset:
                            skipped += 1
                            continue
                        if len(hits) >= raw.max_results:
                            truncated = True
                            break

                        full_content = lines["text"].rstrip("\r\n")
                        content = full_content[:MAX_GREP_HIT_CHARS]
                        if hits and page_chars + len(content) > MAX_GREP_PAGE_CHARS:
                            truncated = True
                            break
                        hit_path = resolver.resolve(path_data["text"])
                        hits.append({
                            "file": hit_path.workspace_membership.display_path,
                            **hit_path.result_fields(),
                            "line": line_number,
                            "content": content,
                            "content_truncated": len(content) < len(full_content),
                            "submatches": [
                                {"start": match["start"], "end": match["end"]}
                                for match in submatches[:MAX_GREP_SUBMATCHES]
                            ],
                            "submatches_truncated": len(submatches) > MAX_GREP_SUBMATCHES,
                        })
                        page_chars += len(content)
                    if not truncated and protocol_error is None:
                        return_code = await proc.wait()
                        stderr = await stderr_task
            except TimeoutError:
                timed_out = True
                stderr = await stop_process()
            else:
                if truncated or protocol_error is not None:
                    stderr = await stop_process()
                    return_code = proc.returncode
        except BaseException:
            cleanup = asyncio.create_task(stop_process())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise

        if timed_out:
            return {
                "ok": False,
                "code": "RG_TIMEOUT",
                "error": f"rg 搜索超过 {GREP_TIMEOUT_SECONDS} 秒",
                **resolved_path.result_fields(),
            }
        if protocol_error is not None:
            return {
                "ok": False,
                "code": "RG_FAILED",
                "error": protocol_error,
                "hint": "搜索结果无法解析，不能据此断定没有匹配；缩小 path 后重试，或改用 glob/read_file。",
                **resolved_path.result_fields(),
            }
        if not truncated and return_code == 2:
            return {
                "ok": False,
                "code": "RG_FAILED",
                "error": stderr or "rg 执行失败",
                **resolved_path.result_fields(),
            }

        return {
            "ok": True,
            "code": "GREP_COMPLETED",
            "path": resolved_path.workspace_membership.display_path,
            **resolved_path.result_fields(),
            "query": raw.query,
            "hits": hits,
            "truncated": truncated,
            "next_offset": raw.offset + len(hits) if truncated else None,
        }

    async def read_file(raw: ReadFileInput) -> dict[str, Any]:
        try:
            resolved_path, err = _require_existing(
                resolver, raw.path, expect="file"
            )
        except EnvironmentInputError as exc:
            return environment_error(exc)
        except OSError as exc:
            return _filesystem_failure("FILE_READ_FAILED", raw.path, exc)
        if err:
            return err
        assert resolved_path is not None
        path = resolved_path.native_path
        relative_path = resolved_path.workspace_membership.display_path
        selected: list[str] = []
        content_length = 0
        end_line = raw.offset - 1
        truncated_by = None
        next_offset = None
        last_seen_line = 0

        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    last_seen_line = line_number
                    if line_number < raw.offset:
                        continue
                    if len(selected) >= raw.limit:
                        truncated_by = "lines"
                        next_offset = line_number
                        break
                    if content_length + len(line) > MAX_READ_CHARS:
                        if selected:
                            truncated_by = "chars"
                            next_offset = line_number
                            break
                        return {
                            "ok": False,
                            "code": "LINE_TOO_LONG",
                            "error": f"第 {line_number} 行超过单次字符上限: {relative_path}",
                            "path": relative_path,
                            "line": line_number,
                            "preview": line[:MAX_READ_CHARS],
                            "max_chars": MAX_READ_CHARS,
                            **resolved_path.result_fields(),
                        }
                    selected.append(line)
                    content_length += len(line)
                    end_line = line_number
        except UnicodeDecodeError:
            return {
                "ok": False,
                "code": "NOT_A_TEXT_FILE",
                "error": f"无法以 UTF-8 文本读取: {relative_path}",
                "path": relative_path,
                **resolved_path.result_fields(),
            }
        except OSError as exc:
            return _filesystem_failure("FILE_READ_FAILED", relative_path, exc)

        if not selected and last_seen_line < raw.offset:
            if not (raw.offset == 1 and last_seen_line == 0):
                return {
                    "ok": False,
                    "code": "OFFSET_OUT_OF_RANGE",
                    "error": f"起始行号超出文件末尾: {relative_path}",
                    "path": relative_path,
                    **resolved_path.result_fields(),
                }

        return {
            "ok": True,
            "code": "FILE_READ",
            "path": relative_path,
            **resolved_path.result_fields(),
            "content": "".join(selected),
            "start_line": raw.offset,
            "end_line": end_line,
            "next_offset": next_offset,
            "truncated": truncated_by is not None,
            "truncated_by": truncated_by,
        }

    return [
        ToolSpec(
            name="glob",
            description=GLOB_DESCRIPTION,
            parameters=PydanticParameters(GlobInput),
            handler=glob,
        ),
        ToolSpec(
            name="grep",
            description=GREP_DESCRIPTION,
            parameters=PydanticParameters(GrepInput),
            handler=grep,
        ),
        ToolSpec(
            name="read_file",
            description=READ_FILE_DESCRIPTION,
            parameters=PydanticParameters(ReadFileInput),
            handler=read_file,
        ),
    ]


def _filesystem_failure(code: str, path: str, exc: OSError) -> dict[str, Any]:
    return {
        "ok": False,
        "code": code,
        "error": f"文件系统操作失败: {type(exc).__name__}: {exc}",
        "path": path,
    }
