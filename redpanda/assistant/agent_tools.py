"""Model tools for landing an Agent's work and syncing what colleagues landed."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from redpanda.runtime import ToolBinding
from redpanda.runtime.dispatcher import AttemptContext


LAND = "land_branch"
SYNC = "sync_branch"
AGENT_GIT_TOOLS = frozenset({LAND, SYNC})

_NO_ARGUMENTS = {"type": "object", "properties": {}, "additionalProperties": False}

AGENT_GIT_SCHEMAS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": LAND,
            "description": (
                "把你分支上已经提交的工作落地到项目的集成分支，同事同步后就能拿到。"
                "未提交的修改不会带走，需要时先提交再落地。"
                "能快进则快进，否则产生一个以你为作者的合并提交。"
                "与集成分支冲突时集成分支不变，结果列出冲突文件，先用 sync_branch 同步并解决冲突再落地。"
                "落地不需要用户批准，适合小步频繁落地。必须单独调用。"
            ),
            "parameters": _NO_ARGUMENTS,
        },
    },
    {
        "type": "function",
        "function": {
            "name": SYNC,
            "description": (
                "把同事已经落地到项目集成分支的工作合进你的分支。尚未落地的同事改动不在其中。"
                "能快进则快进，否则产生一个合并提交。"
                "有冲突时合并停在冲突状态，由你解决后用 git add 和 git commit 完成；"
                "也可以用 git merge --abort 放弃这次同步。"
                "工作区里未提交的改动会被同步覆盖时，同步不执行。必须单独调用。"
            ),
            "parameters": _NO_ARGUMENTS,
        },
    },
]

SessionTransport = Callable[[str, str, dict], Awaitable[object]]


def _land_result(result: Mapping[str, object]) -> dict[str, object]:
    status = result["status"]
    if status == "merged":
        return {"ok": True, "code": "LAND_COMPLETED",
                "data": {"commit": result["head"], "fast_forward": result["fast_forward"]}}
    if status == "unchanged":
        return {"ok": True, "code": "LAND_UNCHANGED", "data": {"commit": result["head"]},
                "hint": "集成分支已经包含你分支上全部已提交的工作。"}
    if status == "conflict":
        return {"ok": False, "code": "LAND_CONFLICT", "data": {"files": list(result["files"])},
                "error": "你的分支与集成分支上已落地的工作冲突，集成分支没有改变。",
                "hint": "先用 sync_branch 同步并解决冲突，再落地。"}
    if status == "blocked":
        return {"ok": False, "code": "LAND_BLOCKED", "data": {"files": list(result["files"])},
                "error": "集成分支那边有不属于任何落地的文件会被这次落地覆盖，落地没有执行，集成分支没有改变。",
                "hint": "这些文件不在你的工作区里，你无法处理。告诉用户落地被这些文件挡住，需要用户清理；你分支上的提交都还在，清理后再落地。"}
    raise ValueError(f"unexpected land status: {status}")


def _sync_result(result: Mapping[str, object]) -> dict[str, object]:
    status = result["status"]
    if status == "merged":
        return {"ok": True, "code": "SYNC_COMPLETED",
                "data": {"commit": result["head"], "fast_forward": result["fast_forward"]}}
    if status == "unchanged":
        return {"ok": True, "code": "SYNC_UNCHANGED", "data": {"commit": result["head"]},
                "hint": "你的分支已经包含集成分支上全部已落地的工作。"}
    if status == "conflict":
        return {"ok": False, "code": "SYNC_CONFLICT", "data": {"files": list(result["files"])},
                "error": "同步有冲突，合并停在冲突状态。",
                "hint": "逐个解决冲突文件里的冲突标记，再用 git add 和 git commit 完成合并；也可以用 git merge --abort 放弃这次同步。"}
    if status == "blocked":
        return {"ok": False, "code": "SYNC_BLOCKED", "data": {"git": result["detail"]},
                "error": "工作区里未提交的改动或未跟踪文件会被同步覆盖，同步没有执行。",
                "hint": "处理 git 列出的文件后再同步。"}
    if status == "in_progress":
        return {"ok": False, "code": "SYNC_MERGE_IN_PROGRESS",
                "error": "还有一次合并没有完成。",
                "hint": "先用 git add 和 git commit 完成它，或用 git merge --abort 放弃，再同步。"}
    raise ValueError(f"unexpected sync status: {status}")


def _no_arguments(name: str) -> dict[str, object]:
    return {"ok": False, "code": "INVALID_ARGUMENT", "error": f"{name} 不接受参数"}


def agent_git_bindings(transport: SessionTransport) -> dict[str, ToolBinding]:
    async def land(context: AttemptContext, arguments: Mapping[str, object]) -> object:
        if arguments:
            return _no_arguments(LAND)
        return _land_result(await transport("agent_land", context.session_id, {}))

    async def sync(context: AttemptContext, arguments: Mapping[str, object]) -> object:
        if arguments:
            return _no_arguments(SYNC)
        return _sync_result(await transport("agent_sync", context.session_id, {}))

    return {LAND: ToolBinding(land), SYNC: ToolBinding(sync)}
