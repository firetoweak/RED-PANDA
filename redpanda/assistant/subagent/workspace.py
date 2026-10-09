"""子工作树的确定性位置与父的验收端口。"""
from base64 import b32encode
from hashlib import sha256
import json
from pathlib import Path

from redpanda.assistant.subagent.subagent import project_delegate_intents, project_failed_delegations
from redpanda.sandbox.registry import WorkspaceRecord
from redpanda.sandbox.versions import WorkspaceVersions
from redpanda.sandbox.worktrees import ReviewWorktrees


def workspace_versions(home, workspace):
    storage = home.state_root / "workspace_views" / sha256(
        str(workspace.task_root.resolve()).encode("utf-8")
    ).hexdigest()
    return WorkspaceVersions(workspace.task_root, storage)


def review_worktrees(home, workspace, *, ref="HEAD", ignore_root=None):
    storage = home.state_root / "subagent_reviews" / sha256(workspace.workspace_id.encode()).hexdigest()
    return ReviewWorktrees(workspace.task_root, storage, ref=ref,
                          ignore_root=ignore_root, excluded_roots=(home.root,))


def child_layout(home, parent_id, child_id):
    child_digest = sha256(child_id.encode("utf-8")).digest()
    # 一个完整摘要包含父子身份，避免两层摘要把 Git 工作目录推过 MAX_PATH。
    identity = sha256(json.dumps([parent_id, child_id], separators=(",", ":")).encode()).digest()
    key = b32encode(identity).decode("ascii").rstrip("=").lower()
    return home.state_root / "subagent_worktrees" / key, f"refs/subagents/{child_digest.hex()}"


def child_workspace(parent, root: Path):
    return WorkspaceRecord(parent.workspace_id, parent.name, root, False, parent.created_at)


class ChildWorkspaceReview:
    def __init__(self, runtime, session_id, versions, home, transport, sandbox):
        self.runtime = runtime
        self.session_id = session_id
        self.versions = versions
        self.home = home
        self.transport = transport
        self.sandbox = sandbox

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
        versions = ReviewWorktrees(self.sandbox.native_path(self.sandbox.root),
                                  self.versions.storage, ref=self.versions.ref,
                                  ignore_root=self.versions.ignore_root, symlinks=False)
        if merge:
            conflicts = await versions.merge(base, child["version"])
            if conflicts:
                return {"ok": False, "code": "MERGE_CONFLICT", "data": {"conflicts": list(conflicts)},
                        "error": "合入冲突，用户的文件没有被改动；可自己修改、放弃，或用 resolve_conflicts_of 再委派解决。"}
            return {"ok": True, "code": "SUBAGENT_MERGED", "data": {"tool_call_id": tool_call_id}}
        data = await versions.compare(base, child["version"], tuple(paths or ()))
        return {"ok": True, "code": "SUBAGENT_CHANGES", "data": data}
