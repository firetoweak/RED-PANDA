"""Task file views and immutable child-file exchange owned by Sandbox."""
from base64 import b32encode
from hashlib import sha256
import json
import re
import shutil

from redpanda.sandbox.registry import WorkspaceRecord
from .operations import workspace_files
from .lifecycle import _sqlite_lock, settled
from .git import ReviewWorktrees, _native_path
from .state import atomic, fields, load


def _child_key(parent_id, child_id):
    identity = sha256(json.dumps([parent_id, child_id], separators=(",", ":")).encode()).digest()
    return b32encode(identity).decode("ascii").rstrip("=").lower()


def child_root(home, parent_id, child_id):
    return home.state_root / "subagent_worktrees" / _child_key(parent_id, child_id)


def child_workspace(parent, root):
    return WorkspaceRecord(parent.workspace_id, parent.name, root, False, parent.created_at)


def _state(path, names):
    value = load(path)
    fields(value, names)
    if any(type(item) is not str or re.fullmatch(r"[0-9a-f]{40}", item) is None for item in value.values()):
        raise ValueError("invalid child file state")
    return value


class ChildFiles:
    def __init__(self, home, workspace):
        self.home = home
        self.workspace = workspace

    def _store(self, parent_id, child_id):
        return self.home.state_root / "file_exchanges" / _child_key(parent_id, child_id)

    def _git(self, parent_id, child_id, *, root=None, symlinks=True):
        return ReviewWorktrees(
            self.workspace.task_root if root is None else root,
            self._store(parent_id, child_id),
            ignore_root=self.workspace.task_root, excluded_roots=(self.home.root,), symlinks=symlinks,
            objects=self.home.state_root / "file_objects" / "objects",
        )

    async def create(self, parent_id, child_id, *, conflict_child_id=None):
        root = child_root(self.home, parent_id, child_id)
        store = self._store(parent_id, child_id)
        store.mkdir(parents=True, exist_ok=True)
        # Only retries of this delegation serialize. Siblings have no shared queue.
        async with _sqlite_lock(store / "creation-lock.sqlite"):
            git = self._git(parent_id, child_id)
            baseline_path = store / "baseline.json"
            if not baseline_path.exists():
                view = workspace_files(self.home, self.workspace)
                base = await view.read(git.snapshot)
                seed = base
                if conflict_child_id is not None:
                    source = self._git(parent_id, conflict_child_id)
                    result = self._result(parent_id, conflict_child_id)
                    seed = await git.seed(base, source, result)
                await settled(atomic, baseline_path, {"base": base, "seed": seed})
            baseline = _state(baseline_path, ("base", "seed"))
            # The host baseline is now immutable; copying does not hold the parent's read lock.
            await git.materialize(root, baseline["seed"])
        return root

    def _result(self, parent_id, child_id):
        return _state(self._store(parent_id, child_id) / "result.json", ("base", "version"))

    async def finish(self, parent_id, child_id):
        result_path = self._store(parent_id, child_id) / "result.json"
        if result_path.exists():
            self._result(parent_id, child_id)
            return
        root = child_root(self.home, parent_id, child_id)
        view = workspace_files(self.home, child_workspace(self.workspace, root))
        async def freeze():
            if result_path.exists():
                self._result(parent_id, child_id)
                return
            baseline = _state(result_path.with_name("baseline.json"), ("base", "seed"))
            paths = await settled(view.changed_paths)
            git = self._git(parent_id, child_id, root=root)
            version = await git.record(baseline["seed"], paths)
            await settled(atomic, result_path, {"base": baseline["base"], "version": version})
        await view.freeze(freeze)

    async def compare(self, parent_id, child_id, paths=(), *, offset=0):
        result = self._result(parent_id, child_id)
        return await self._git(parent_id, child_id).compare(result["base"], result["version"], tuple(paths), offset=offset)

    async def merge(self, parent_id, child_id, view):
        result = self._result(parent_id, child_id)
        git = self._git(parent_id, child_id, root=view.native_path(view.root), symlinks=False)
        return await git.merge(result["base"], result["version"])

    async def discard(self, parent_id, child_ids):
        for child_id in child_ids:
            root = child_root(self.home, parent_id, child_id)
            view = workspace_files(self.home, child_workspace(self.workspace, root))
            for path, boundary in (
                (root, self.home.state_root / "subagent_worktrees"),
                (self._store(parent_id, child_id), self.home.state_root / "file_exchanges"),
                (view.storage, self.home.state_root / "workspace_views"),
            ):
                if not _native_path(path).is_relative_to(_native_path(boundary)):
                    raise ValueError("child cleanup escaped product data root")
                if path.exists():
                    await settled(shutil.rmtree, path)
