"""Parent ownership and acceptance of delegated file results."""
from redpanda.assistant.subagent.subagent import project_delegate_intents, project_failed_delegations
from redpanda.sandbox.files import operation_id


class ChildWorkspaceReview:
    def __init__(self, runtime, session_id, files, transport, sandbox):
        self.runtime = runtime
        self.session_id = session_id
        self.files = files
        self.transport = transport
        self.sandbox = sandbox

    async def review(self, command_id, tool_call_id, paths=None, *, merge=False, offset=0):
        try:
            return await self._review(command_id, tool_call_id, paths, merge=merge, offset=offset)
        except OSError:
            return {"ok": False, "code": "CHILD_WORKSPACE_FAILED",
                    "error": "无法读取或合入子任务文件；请检查磁盘空间、文件占用和访问权限后重试。"}

    async def _review(self, command_id, tool_call_id, paths, *, merge, offset):
        events = await self.runtime.snapshot(self.session_id)
        intent = next((item for item in project_delegate_intents(events)
                       if item.command_id == tool_call_id), None)
        if intent is None or intent.child_session_id in project_failed_delegations(events):
            return {"ok": False, "code": "UNKNOWN_DELEGATE", "error": "不是当前会话已提交的委派调用。"}
        # Settle the child before taking any parent writer/publication lock.
        child = await self.transport("prepare_child_files", intent.child_session_id,
                                     {"parent_session_id": self.session_id})
        if not child["ok"]:
            return child
        if merge:
            async def apply():
                try:
                    result = await self.files.merge(self.session_id, intent.child_session_id, self.sandbox)
                except OSError:
                    return {"ok": False, "code": "CHILD_WORKSPACE_FAILED",
                            "error": "无法合入子任务文件；请检查磁盘空间、文件占用和访问权限后重试。"}
                if result.conflicts:
                    return {"ok": False, "code": "MERGE_CONFLICT", "data": {"conflicts": list(result.conflicts)},
                            "error": "合入冲突，用户的文件没有被改动；可自己修改、放弃，或用 resolve_conflicts_of 再委派解决。"}
                return {"ok": True, "code": "SUBAGENT_MERGED",
                        "data": {"tool_call_id": tool_call_id, "changed_files": list(result.changed_paths[:100]),
                                 "changed_count": len(result.changed_paths), "truncated": len(result.changed_paths) > 100}}
            return await self.sandbox.execute(operation_id(self.session_id, command_id), apply)
        data = await self.files.compare(self.session_id, intent.child_session_id, tuple(paths or ()), offset=offset)
        return {"ok": True, "code": "SUBAGENT_CHANGES", "data": data}
