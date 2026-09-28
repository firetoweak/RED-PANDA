"""子工作树的确定性位置与父的验收端口。"""
from base64 import b32encode
from hashlib import sha256
from pathlib import Path

from helperme.assistant.subagent.subagent import project_delegate_intents, project_failed_delegations
from helperme.sandbox.registry import WorkspaceRecord
from helperme.sandbox.versions import WorkspaceVersions


def workspace_versions(home, workspace, *, ref="HEAD", ignore_root=None):
    storage = home.state_root / "workspace_versions" / sha256(
        workspace.workspace_id.encode("utf-8")
    ).hexdigest()
    return WorkspaceVersions(workspace.task_root, storage, ref=ref,
                             ignore_root=ignore_root, excluded_roots=(home.root,))


def child_layout(home, parent_id, child_id):
    parent_digest = sha256(parent_id.encode("utf-8")).digest()
    child_digest = sha256(child_id.encode("utf-8")).digest()
    # 完整摘要用 Base32 缩短目录，避免 Windows 常见临时目录下超过路径上限。
    parent_key = b32encode(parent_digest).decode("ascii").rstrip("=").lower()
    child_key = b32encode(child_digest).decode("ascii").rstrip("=").lower()
    return home.state_root / "subagent_worktrees" / parent_key / child_key, f"refs/subagents/{child_digest.hex()}"


def child_workspace(parent, root: Path):
    return WorkspaceRecord(parent.workspace_id, parent.name, root, False, parent.created_at)


class ChildWorkspaceReview:
    def __init__(self, runtime, session_id, versions, home, transport):
        self.runtime = runtime
        self.session_id = session_id
        self.versions = versions
        self.home = home
        self.transport = transport

    async def review(self, tool_call_id, paths=None, *, merge=False):
        try:
            return await self._review(tool_call_id, paths, merge=merge)
        except OSError as error:
            return {"ok": False, "code": "CHILD_WORKSPACE_FAILED", "error": str(error)}

    async def _review(self, tool_call_id, paths, *, merge):
        events = await self.runtime.snapshot(self.session_id)
        intent = next((item for item in project_delegate_intents(events)
                       if item.command_id == tool_call_id), None)
        if intent is None or intent.child_session_id in project_failed_delegations(events):
            return {"ok": False, "code": "UNKNOWN_DELEGATE", "error": "不是当前会话已提交的委派调用。"}
        child = await self.transport("child_workspace_version", intent.child_session_id,
                                     {"parent_session_id": self.session_id})
        if not child["ok"]:
            return child
        _, ref = child_layout(self.home, self.session_id, intent.child_session_id)
        base = ref + "-base"
        if merge:
            conflicts = await self.versions.merge(base, child["version"])
            if conflicts:
                return {"ok": False, "code": "MERGE_CONFLICT", "data": {"conflicts": list(conflicts)},
                        "error": "合入冲突，用户的文件没有被改动；可自己修改、放弃，或用 resolve_conflicts_of 再委派解决。"}
            return {"ok": True, "code": "SUBAGENT_MERGED", "data": {"tool_call_id": tool_call_id}}
        data = await self.versions.compare(base, child["version"], tuple(paths or ()))
        return {"ok": True, "code": "SUBAGENT_CHANGES", "data": data}
