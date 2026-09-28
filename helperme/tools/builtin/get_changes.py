from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from helperme.sandbox.api import (
    EnvironmentBinding,
    environment_error,
)
from helperme.sandbox.workspace import EnvironmentInputError
from helperme.tools.spec import PydanticParameters, ToolSpec


GET_CHANGES_DESCRIPTION = """
用途：读取指定路径所属 Git 仓库的状态，以及 HEAD 到最终工作树的 tracked 差异，用于验证文件修改和最终总结。
何时使用：修改后核对磁盘实际变化、最终回答前确认声明与 diff 一致时使用；它只验证，不代替 apply_patch/replace_all/write_file，也不用于查找文件或内容。
关键限制：相对 path 从工作区开始，绝对 path 按本机路径规则；只支持 Git 仓库；diff 覆盖 staged 与 unstaged 的 tracked 最终内容，但不包含 untracked 和 binary 正文。content_complete=false 或 limitations 非空表示仍需 read_file 等证据补全。
失败/截断后：truncated=true 时缩小 path 后重查；VERIFICATION_BACKEND_UNAVAILABLE 时应明确无法用 Git 验证，不能声称无改动；GIT_CHANGES_FAILED 时不能声称已核对改动，改用 read_file 直接读取相关文件核对，并如实说明未能用 Git 验证。
""".strip()


MAX_DIFF_CHARS = 120_000
DIFF_HEAD_CHARS = 60_000


class GetChangesInput(BaseModel):
    path: str = Field(default=".", description="要检查的文件或目录；相对路径从工作区开始")


async def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--no-optional-locks",
        "-c", "diff.autoRefreshIndex=false",
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        raise
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


class GitVerificationError(OSError):
    pass


async def _gather_git_stdout(
    commands: tuple[list[str], ...],
    cwd: Path,
) -> tuple[str, ...]:
    results = await asyncio.gather(
        *(_run_git(command, cwd) for command in commands),
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, BaseException)]
    unexpected = [error for error in errors if not isinstance(error, OSError)]
    if len(unexpected) == 1:
        raise unexpected[0]
    if unexpected:
        raise BaseExceptionGroup("多个 Git 验证命令异常", unexpected)
    if errors:
        raise errors[0]

    outputs: list[str] = []
    for result in results:
        returncode, stdout, stderr = result
        if returncode != 0:
            raise GitVerificationError(
                stderr.strip() or "Git 子命令返回非零状态"
            )
        outputs.append(stdout)
    return tuple(outputs)


def create_get_changes_specs(binding: EnvironmentBinding) -> list[ToolSpec]:
    resolver = binding.resolver

    async def get_changes(raw: GetChangesInput) -> dict[str, Any]:
        try:
            resolved_target = resolver.resolve(raw.path)
        except EnvironmentInputError as exc:
            return environment_error(exc)
        target = resolved_target.native_path
        try:
            discovery_cwd = target if target.is_dir() else target.parent
        except OSError as exc:
            return _git_failure(resolved_target, exc)

        try:
            repo_returncode, repo_stdout, _ = await _run_git(
                ["rev-parse", "--show-toplevel"],
                discovery_cwd,
            )
        except OSError as exc:
            return _git_failure(resolved_target, exc)
        if repo_returncode != 0:
            return {
                "ok": False,
                "code": "VERIFICATION_BACKEND_UNAVAILABLE",
                "source": "no_git_backend",
                "changed": None,
                "status": "",
                "diff": "",
                "diff_basis": None,
                "baseline_revision": None,
                "content_complete": False,
                "untracked_paths": [],
                "binary_paths": [],
                "limitations": ["GIT_REPOSITORY_UNAVAILABLE"],
                "truncated": False,
                "diff_total_chars": 0,
                "diff_omitted_chars": 0,
                **resolved_target.result_fields(),
                "message": "当前路径不属于 Git 工作区。",
            }

        try:
            repo_path = repo_stdout.strip()
            if not repo_path:
                raise ValueError("git rev-parse 返回空路径")
            resolved_repo = resolver.resolve(repo_path)
        except EnvironmentInputError as exc:
            return environment_error(exc)
        repo_root = resolved_repo.native_path
        try:
            path_args = ["--", target.relative_to(repo_root).as_posix()]
        except ValueError as exc:
            return _git_failure(resolved_target, exc)

        try:
            head_returncode, head_stdout, head_stderr = await _run_git(
                ["rev-parse", "--verify", "HEAD"],
                repo_root,
            )
        except OSError as exc:
            return _git_failure(resolved_target, exc)
        if head_returncode != 0:
            return await _read_without_head(
                resolved_target,
                resolved_repo,
                repo_root,
                path_args,
                head_stderr,
            )

        try:
            status, full_diff, numstat, untracked = await _gather_git_stdout(
                (
                    [
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                        *path_args,
                    ],
                    [
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--no-color",
                        "HEAD",
                        *path_args,
                    ],
                    [
                        "diff",
                        "--numstat",
                        "--no-renames",
                        "--no-textconv",
                        "-z",
                        "HEAD",
                        *path_args,
                    ],
                    [
                        "ls-files",
                        "--others",
                        "--exclude-standard",
                        "-z",
                        *path_args,
                    ],
                ),
                repo_root,
            )
            diff, truncated, omitted_chars = _bounded_diff(full_diff)
            untracked_paths = _nul_paths(untracked)
            binary_paths = _binary_paths(numstat)
        except OSError as exc:
            return _git_failure(resolved_target, exc)
        limitations: list[str] = []
        if untracked_paths:
            limitations.append("UNTRACKED_CONTENT_NOT_INCLUDED")
        if binary_paths:
            limitations.append("BINARY_CONTENT_NOT_INCLUDED")
        if truncated:
            limitations.append("DIFF_TRUNCATED")
        return {
            "ok": True,
            "code": "CHANGES_READ",
            "source": "git",
            **resolved_target.result_fields(),
            "repository_location": resolved_repo.location.to_dict(),
            "changed": bool(status.strip()),
            "status": status,
            "diff": diff,
            "diff_basis": "HEAD_TO_WORKTREE",
            "baseline_revision": head_stdout.strip(),
            "content_complete": not limitations,
            "untracked_paths": untracked_paths,
            "binary_paths": binary_paths,
            "limitations": limitations,
            "truncated": truncated,
            "diff_total_chars": len(full_diff),
            "diff_omitted_chars": omitted_chars,
        }

    return [
        ToolSpec(
            name="get_changes",
            description=GET_CHANGES_DESCRIPTION,
            parameters=PydanticParameters(GetChangesInput),
            handler=get_changes,
        )
    ]


def _git_failure(resolved_target, exc: BaseException) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "GIT_CHANGES_FAILED",
        "source": "git",
        "changed": None,
        "status": "",
        "diff": "",
        "diff_basis": None,
        "baseline_revision": None,
        "content_complete": False,
        "untracked_paths": [],
        "binary_paths": [],
        "limitations": ["GIT_CHANGES_FAILED"],
        "truncated": False,
        "diff_total_chars": 0,
        "diff_omitted_chars": 0,
        **resolved_target.result_fields(),
        "message": f"Git 验证失败: {type(exc).__name__}: {exc}",
    }


async def _read_without_head(
    resolved_target,
    resolved_repo,
    repo_root: Path,
    path_args: list[str],
    head_stderr: str,
) -> dict[str, Any]:
    try:
        status, untracked = await _gather_git_stdout(
            (
                ["status", "--porcelain=v1", "--untracked-files=all", *path_args],
                ["ls-files", "--others", "--exclude-standard", "-z", *path_args],
            ),
            repo_root,
        )
        untracked_paths = _nul_paths(untracked)
    except OSError as exc:
        return _git_failure(resolved_target, exc)
    limitations = ["HEAD_BASELINE_UNAVAILABLE"]
    if untracked_paths:
        limitations.append("UNTRACKED_CONTENT_NOT_INCLUDED")
    return {
        "ok": True,
        "code": "CHANGES_READ",
        "source": "git",
        **resolved_target.result_fields(),
        "repository_location": resolved_repo.location.to_dict(),
        "changed": bool(status.strip()),
        "status": status,
        "diff": "",
        "diff_basis": None,
        "baseline_revision": None,
        "content_complete": False,
        "untracked_paths": untracked_paths,
        "binary_paths": [],
        "limitations": limitations,
        "truncated": False,
        "diff_total_chars": 0,
        "diff_omitted_chars": 0,
        "message": (
            "仓库尚无 HEAD，无法生成基线到最终工作树的正文差异。"
            + (f" Git: {head_stderr.strip()}" if head_stderr.strip() else "")
        ),
    }


def _nul_paths(value: str) -> list[str]:
    return [path for path in value.split("\x00") if path]


def _binary_paths(numstat: str) -> list[str]:
    paths: list[str] = []
    for record in _nul_paths(numstat):
        fields = record.split("\t", 2)
        if len(fields) != 3:
            raise GitVerificationError("git diff --numstat 返回无效记录")
        added, deleted, path = fields
        if added == "-" and deleted == "-":
            paths.append(path)
    return paths


def _bounded_diff(value: str) -> tuple[str, bool, int]:
    if len(value) <= MAX_DIFF_CHARS:
        return value, False, 0
    omitted = len(value) - MAX_DIFF_CHARS
    tail_chars = MAX_DIFF_CHARS - DIFF_HEAD_CHARS
    marker = f"\n... [diff 截断 {omitted} 字符] ...\n"
    return value[:DIFF_HEAD_CHARS] + marker + value[-tail_chars:], True, omitted
