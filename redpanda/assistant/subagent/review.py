"""Parent ownership and acceptance of delegated file results."""
from redpanda.assistant.subagent.subagent import project_delegate_intents, project_failed_delegations


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
        if status == "failed":
            return {"ok": False, "code": "MERGE_FAILED", "data": {"git": result["detail"]},
                    "error": "合入时 Git 报错，合入没有成功。",
                    "hint": "按 Git 原文处理后再合入；处理不了就把原文告诉用户。"}
        return {"ok": False, "code": "MERGE_IN_PROGRESS", "error": "你的工作区还有一次合并没有完成。",
                "hint": "先用 git add 和 git commit 完成它，或用 git merge --abort 放弃，再合入。"}
