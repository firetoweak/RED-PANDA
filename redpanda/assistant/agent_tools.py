"""Model tools for a Bot's projects: linking them, landing its work and syncing what colleagues landed."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from redpanda.runtime import ToolBinding
from redpanda.runtime.dispatcher import AttemptContext


LAND = "land_branch"
SYNC = "sync_branch"
LINK = "link_project"
UNLINK = "unlink_project"
AGENT_GIT_TOOLS = frozenset({LAND, SYNC, LINK, UNLINK})
# Each merges into a worktree, so nothing else runs beside it in one step.
EXCLUSIVE_PROJECT_TOOLS = frozenset({LAND, SYNC})

_PROJECT = {"type": "string", "minLength": 1, "description": "你挂上的项目名。"}


def _project_only(description: str) -> dict[str, object]:
    return {"type": "object", "properties": {"project": {**_PROJECT, "description": description}},
            "required": ["project"], "additionalProperties": False}


AGENT_GIT_SCHEMAS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": LINK,
            "description": (
                "把一个代码项目挂到你这里：在你个人目录的 projects 下给你准备一份独立工作树，"
                "在你自己的分支上，从这个项目已落地的工作开始。可以同时挂多个项目。"
                "用户提到项目名时先用 project 直接挂；没有这个名字时，结果会列出已登记的项目名。"
                "只有用户给出路径、或项目确实还没登记时，才用 path 给出本地 Git 仓库根目录的绝对路径，"
                "需要用户批准，批准后原地登记再挂上。project 和 path 只给一个。"
                "成功时返回项目名和工作树目录，之后用这个项目名调用 land_branch、sync_branch、unlink_project 和 delegate。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "minLength": 1, "description": "已登记项目的名字。"},
                    "path": {"type": "string", "minLength": 1,
                             "description": "还没登记的本地 Git 仓库根目录的绝对路径，仓库至少要有一个提交。"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": UNLINK,
            "description": (
                "解除挂在你这里的项目，移除你在其中的工作树和分支，不影响其他 Bot 和项目本身。"
                "只有工作树里没有未提交的改动、分支上没有未落地的提交、没有进行中的合并，"
                "也没有委派到这个项目还没交回的子 Agent 时才会解除；否则什么都不改，结果列出原因。"
            ),
            "parameters": _project_only("要解除的项目名。"),
        },
    },
    {
        "type": "function",
        "function": {
            "name": LAND,
            "description": (
                "把你在这个项目分支上已经提交的工作落地到项目的集成分支，同事同步后就能拿到。"
                "未提交的修改不会带走，需要时先提交再落地。"
                "能快进则快进，否则产生一个以你为作者的合并提交。"
                "与集成分支冲突时集成分支不变，结果列出冲突文件，先用 sync_branch 同步并解决冲突再落地。"
                "落地不需要用户批准，适合小步频繁落地。必须单独调用。"
            ),
            "parameters": _project_only("要落地的项目名。"),
        },
    },
    {
        "type": "function",
        "function": {
            "name": SYNC,
            "description": (
                "把同事已经落地到这个项目集成分支的工作合进你的分支。尚未落地的同事改动不在其中。"
                "能快进则快进，否则产生一个合并提交。"
                "有冲突时合并停在冲突状态，由你在这个项目的工作树里解决后用 git add 和 git commit 完成；"
                "也可以用 git merge --abort 放弃这次同步。"
                "工作区里未提交的改动会被同步覆盖时，同步不执行。必须单独调用。"
            ),
            "parameters": _project_only("要同步的项目名。"),
        },
    },
]

SessionTransport = Callable[[str, str, dict], Awaitable[object]]


def _land_result(result: Mapping[str, object]) -> dict[str, object]:
    if result.get("ok") is False:
        return dict(result)
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
                "error": "集成分支那边有不属于任何落地的改动挡住了这次落地，落地没有执行，集成分支没有改变。",
                "hint": "这些文件不在你的工作区里，你无法处理。告诉用户落地被这些文件挡住，需要用户清理；你分支上的提交都还在，清理后再落地。"}
    if status == "failed":
        return {"ok": False, "code": "LAND_FAILED", "data": {"git": result["detail"]},
                "error": "落地时 Git 报错，落地没有成功。",
                "hint": "出错的位置不在你的工作区里，你无法处理。把 Git 原文告诉用户，请用户处理后再落地；你分支上的提交都还在。"}
    raise ValueError(f"unexpected land status: {status}")


def _sync_result(result: Mapping[str, object]) -> dict[str, object]:
    if result.get("ok") is False:
        return dict(result)
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
    if status == "failed":
        return {"ok": False, "code": "SYNC_FAILED", "data": {"git": result["detail"]},
                "error": "同步时 Git 报错，同步没有成功。",
                "hint": "按 Git 原文处理后再同步；处理不了就把原文告诉用户。"}
    if status == "in_progress":
        return {"ok": False, "code": "SYNC_MERGE_IN_PROGRESS",
                "error": "还有一次合并没有完成。",
                "hint": "先用 git add 和 git commit 完成它，或用 git merge --abort 放弃，再同步。"}
    raise ValueError(f"unexpected sync status: {status}")


def _project_argument(name: str, arguments: Mapping[str, object]) -> str | dict[str, object]:
    project = arguments.get("project")
    if set(arguments) != {"project"} or type(project) is not str or not project.strip():
        return {"ok": False, "code": "INVALID_ARGUMENT", "error": f"{name} 只接受非空的 project。"}
    return project


def _link_needs_approval(arguments: Mapping[str, object]) -> bool:
    """Only a path the model chose registers a new project; naming a registered one does not."""
    return arguments.get("path") is not None


def agent_git_bindings(transport: SessionTransport) -> dict[str, ToolBinding]:
    async def land(context: AttemptContext, arguments: Mapping[str, object]) -> object:
        project = _project_argument(LAND, arguments)
        if isinstance(project, dict):
            return project
        return _land_result(await transport("agent_land", context.session_id, {"project": project}))

    async def sync(context: AttemptContext, arguments: Mapping[str, object]) -> object:
        project = _project_argument(SYNC, arguments)
        if isinstance(project, dict):
            return project
        return _sync_result(await transport("agent_sync", context.session_id, {"project": project}))

    async def link(context: AttemptContext, arguments: Mapping[str, object]) -> object:
        given = {key: value for key, value in arguments.items() if value is not None}
        if (len(given) != 1 or not set(given) <= {"project", "path"}
                or type(next(iter(given.values()))) is not str or not next(iter(given.values())).strip()):
            return {"ok": False, "code": "INVALID_ARGUMENT",
                    "error": "link_project 需要且只需要 project 或 path 其中一个非空值。"}
        return await transport("agent_link", context.session_id, {**given, "delivery_id": context.command_id})

    async def unlink(context: AttemptContext, arguments: Mapping[str, object]) -> object:
        project = _project_argument(UNLINK, arguments)
        if isinstance(project, dict):
            return project
        return await transport("agent_unlink", context.session_id,
                               {"project": project, "delivery_id": context.command_id})

    return {
        LAND: ToolBinding(land), SYNC: ToolBinding(sync),
        LINK: ToolBinding(link, requires_authorization=_link_needs_approval),
        UNLINK: ToolBinding(unlink),
    }
