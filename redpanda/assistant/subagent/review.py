"""Parent ownership and acceptance of delegated file results."""
from redpanda.assistant.subagent.subagent import project_delegate_intents, project_failed_delegations
from redpanda.filesystem import operation_id


async def _returned_intent(runtime, session_id, transport, tool_call_id):
    """The committed, returned delegation behind `tool_call_id`, or a known tool result."""
    events = await runtime.snapshot(session_id)
    intent = next((item for item in project_delegate_intents(events)
                   if item.command_id == tool_call_id), None)
    if intent is None or intent.child_session_id in project_failed_delegations(events):
        return None, {"ok": False, "code": "UNKNOWN_DELEGATE", "error": "不是当前会话已提交的委派调用。"}
    # Settle the child before taking any parent writer/publication lock.
    child = await transport("prepare_child_files", intent.child_session_id,
                            {"parent_session_id": session_id})
    return intent, (None if child["ok"] else child)


class GitChildReview:
    """Delegated results are branches; the Host compares and merges them into the parent branch."""

    def __init__(self, runtime, session_id, transport):
        self.runtime = runtime
        self.session_id = session_id
        self.transport = transport

    async def review(self, command_id, tool_call_id, paths=None, *, merge=False, offset=0):
        intent, failure = await _returned_intent(self.runtime, self.session_id, self.transport, tool_call_id)
        if failure is not None:
            return failure
        result = await self.transport("agent_child_review", self.session_id, {
            "child_session_id": intent.child_session_id, "paths": list(paths or ()),
            "offset": offset, "merge": merge,
        })
        if not merge:
            return {"ok": True, "code": "SUBAGENT_CHANGES", "data": result}
        status, changed = result["status"], result["changed_files"]
        if status == "merged" or status == "unchanged":
            return {"ok": True, "code": "SUBAGENT_MERGED",
                    "data": {"tool_call_id": tool_call_id, "changed_files": changed[:100],
                             "changed_count": len(changed), "truncated": len(changed) > 100}}
        if status == "conflict":
            return {"ok": False, "code": "MERGE_CONFLICT", "data": {"conflicts": list(result["files"])},
                    "error": "合入冲突，你的分支没有被改动；可自己修改、放弃，或用 resolve_conflicts_of 再委派解决。"}
        if status == "blocked":
            return {"ok": False, "code": "MERGE_BLOCKED", "data": {"git": result["detail"]},
                    "error": "工作区里未提交的改动或未跟踪文件会被合入覆盖，合入没有执行。",
                    "hint": "处理 git 列出的文件后再合入。"}
        return {"ok": False, "code": "MERGE_IN_PROGRESS", "error": "你的工作区还有一次合并没有完成。",
                "hint": "先用 git add 和 git commit 完成它，或用 git merge --abort 放弃，再合入。"}


class ChildWorkspaceReview:
    def __init__(self, runtime, session_id, files, transport, filesystem):
        self.runtime = runtime
        self.session_id = session_id
        self.files = files
        self.transport = transport
        self.filesystem = filesystem

    async def review(self, command_id, tool_call_id, paths=None, *, merge=False, offset=0):
        try:
            return await self._review(command_id, tool_call_id, paths, merge=merge, offset=offset)
        except OSError:
            return {"ok": False, "code": "CHILD_WORKSPACE_FAILED",
                    "error": "无法读取或合入子任务文件；可稍后重试一次；仍失败请告诉用户检查磁盘空间、文件占用和权限。"}

    async def _review(self, command_id, tool_call_id, paths, *, merge, offset):
        intent, failure = await _returned_intent(self.runtime, self.session_id, self.transport, tool_call_id)
        if failure is not None:
            return failure
        if merge:
            async def apply():
                try:
                    result = await self.files.merge(self.session_id, intent.child_session_id, self.filesystem)
                except OSError:
                    return {"ok": False, "code": "CHILD_WORKSPACE_FAILED",
                            "error": "无法合入子任务文件；可稍后重试一次；仍失败请告诉用户检查磁盘空间、文件占用和权限。"}
                if result.conflicts:
                    return {"ok": False, "code": "MERGE_CONFLICT", "data": {"conflicts": list(result.conflicts)},
                            "error": "合入冲突，用户的文件没有被改动；可自己修改、放弃，或用 resolve_conflicts_of 再委派解决。"}
                return {"ok": True, "code": "SUBAGENT_MERGED",
                        "data": {"tool_call_id": tool_call_id, "changed_files": list(result.changed_paths[:100]),
                                 "changed_count": len(result.changed_paths), "truncated": len(result.changed_paths) > 100}}
            return await self.filesystem.execute(operation_id(self.session_id, command_id), apply)
        data = await self.files.compare(self.session_id, intent.child_session_id, tuple(paths or ()), offset=offset)
        return {"ok": True, "code": "SUBAGENT_CHANGES", "data": data}
