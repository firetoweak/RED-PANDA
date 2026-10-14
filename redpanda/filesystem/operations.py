"""Operation references and native projections; no workspace snapshots or Git rollback."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .lifecycle import _sqlite_lock, settled
from .file_view.client import native_executable

INITIAL = "initial"

def validate_version(version: str) -> None:
    if type(version) is not str or (version != INITIAL and re.fullmatch(r"[0-9a-f]{64}", version) is None):
        raise ValueError("invalid file operation reference")

def operation_id(session_id: str, command_id: str) -> str:
    return sha256(json.dumps([session_id, command_id], separators=(",", ":")).encode()).hexdigest()

class UnknownWorkspaceVersion(ValueError):
    pass

class FileSystemUnavailable(OSError):
    """The native file service is unavailable."""

class WorkspaceRestoreFailed(OSError):
    def __init__(self, before_version: str, error: str, paths: tuple[str, ...] = ()) -> None:
        super().__init__(error)
        self.before_version = before_version
        self.paths = paths

@dataclass(frozen=True)
class WorkspaceRestore:
    before_version: str
    version: str
    changed_paths: tuple[str, ...] = ()
    preserved_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()

def clear_stale_mounts(home) -> None:
    """Linux：文件服务进程被 kill -9 后，它的 FUSE 挂载点只剩断开的连接，启动时摘掉。

    只处理 workspace_views 下自己的挂载点，且只认 ENOTCONN：活着的挂载不碰。
    """
    if sys.platform != "linux":
        return
    views = home.state_root / "workspace_views"
    if not views.is_dir():
        return
    # glob 会把 ENOTCONN 的挂载点当成不存在，所以逐个 view 拼路径。
    for view in views.iterdir():
        point = view / "mount"
        try:
            # 断开的挂载点 stat 仍可能成功，读目录才报 ENOTCONN。
            os.listdir(point)
        except OSError as error:
            if error.errno == errno.ENOTCONN:
                subprocess.run(["fusermount3", "-u", "-z", str(point)], check=False)


def workspace_files(home, workspace):
    storage = home.state_root / "workspace_views" / sha256(
        str(workspace.task_root.resolve()).encode("utf-8")
    ).hexdigest()
    return WorkspaceFiles(workspace.task_root, storage)


class WorkspaceFiles:
    """One ordered operation history per task root, shared by its Session workers."""
    def __init__(self, root: Path, storage: Path, *, executable: Path | None = None):
        self.root = root.resolve()
        self.storage = storage.resolve()
        if os.name == "nt" and not str(self.storage).startswith("\\\\?\\"):
            self.storage = Path("\\\\?\\" + str(self.storage))
        self.executable = executable
        self._mount = ContextVar(f"projection:{self.storage}", default=None)
        self._serial = asyncio.Lock()

    def native_path(self, logical: Path) -> Path:
        mount = self._mount.get()
        if mount is None:
            raise RuntimeError("task filesystem access outside a file operation")
        return mount / logical.relative_to(self.root)

    def logical_path(self, native: Path) -> Path:
        mount = self._mount.get()
        if mount is None:
            raise RuntimeError("task filesystem access outside a file operation")
        return self.root / native.relative_to(mount) if native.is_relative_to(mount) else native

    @asynccontextmanager
    async def _owner(self):
        async with self._serial:
            self.storage.mkdir(parents=True, exist_ok=True)
            async with _sqlite_lock(self.storage / "operation-lock.sqlite"):
                yield

    async def read(self, callback):
        """Read published files concurrently; never expose an executing candidate."""
        from redpanda.filesystem.file_view.publication import unfinished
        self.storage.mkdir(parents=True, exist_ok=True)
        async with _sqlite_lock(self.storage / "publication-lock.sqlite", shared=True):
            if await settled(unfinished, self.storage):
                raise RuntimeError("unfinished host transaction requires reconciliation")
            token = self._mount.set(self.root)
            try:
                return await callback()
            finally:
                self._mount.reset(token)

    async def changes(self, path: Path | None = None, *, operations=None, offset: int = 0, text_offset: int = 0) -> dict:
        """Verify current results of this workspace's published file operations."""
        from .changes import attribution
        target = self.root if path is None else path
        target.relative_to(self.root)
        from functools import partial
        return await self.read(lambda: settled(partial(attribution, self.root, self.storage, target,
                                                      operations=operations, offset=offset, text_offset=text_offset)))

    async def review(self, operations, *, path=None, offset=0):
        from functools import partial
        from .review import review_changes

        return await self.read(lambda: settled(partial(
            review_changes, self.root, self.storage, operations, path=path, offset=offset)))

    def _history(self):
        head = self.storage / "HEAD"
        if not head.exists(): return []
        from .state import fields, load
        from .file_view.publication import command_id
        import uuid
        next_commit = head.read_text(encoding="utf-8")
        operations, seen = [], set()
        while next_commit is not None:
            if str(uuid.UUID(next_commit)) != next_commit or next_commit in seen:
                raise ValueError("invalid file commit chain")
            seen.add(next_commit)
            commit = load(self.storage / "commits" / (next_commit + ".json"))
            fields(commit, ("previous", "digest", "command_id", "kind"))
            if commit["kind"] == "accept":
                command_id(commit["command_id"])
                validate_version(commit["command_id"])
                if commit["command_id"] == INITIAL or type(commit["digest"]) is not str or re.fullmatch(r"[0-9a-f]{64}", commit["digest"]) is None:
                    raise ValueError("invalid accepted file commit")
                operations.append(commit["command_id"])
            elif commit["kind"] != "rebase" or commit["digest"] is not None or commit["command_id"] is not None:
                raise ValueError("invalid file commit")
            next_commit = commit["previous"]
        return list(reversed(operations))

    async def record(self) -> str:
        async def recorded():
            history = await settled(self._history)
            return history[-1] if history else INITIAL
        # Only a completed publication is a file boundary. Reconcile known
        # accepted effects just as child-result freezing does; never rerun tools.
        return await self.freeze(recorded)

    async def freeze(self, callback):
        """Wait for this view's writer, then consume its published state."""
        async with self._owner():
            from .state import fields, load
            from .file_view.publication import unfinished
            import uuid
            def pending():
                head = self.storage / "HEAD"
                if unfinished(self.storage):
                    return True
                if not head.exists():
                    return False
                token = head.read_text(encoding="utf-8")
                if str(uuid.UUID(token)) != token:
                    raise ValueError("invalid file commit reference")
                commit = load(self.storage / "commits" / (token + ".json"))
                fields(commit, ("previous", "digest", "command_id", "kind"))
                if commit["kind"] not in ("accept", "rebase"):
                    raise ValueError("invalid file commit kind")
                return commit["kind"] == "accept"
            if await settled(pending):
                client = self._client()
                try:
                    await self._publish_pending(client)
                finally:
                    await settled(client.close)
            return await self.read(callback)

    def changed_paths(self):
        """Paths touched by accepted operations, including restoration effects."""
        from pathlib import PurePosixPath
        from .state import fields, load
        from .file_view.publication import valid_image, valid_operation
        paths = set()
        for identity in self._history():
            receipt = load(self.storage / "commands" / identity / "sealed.json")
            fields(receipt, ("version", "command_id", "parent_commit", "digest", "changes", "operation"))
            if receipt["version"] != 3 or receipt["command_id"] != identity:
                raise ValueError("invalid accepted receipt")
            valid_operation(receipt["operation"])
            for change in receipt["changes"]:
                fields(change, ("path", "before", "after"))
                before, after = valid_image(change["before"]), valid_image(change["after"])
                path = PurePosixPath(change["path"])
                if not path.is_absolute() or ".." in path.parts:
                    raise ValueError("invalid evidence path")
                # Directory metadata is not a reason to rehash its unchanged children.
                if before["kind"] == after["kind"] == "directory":
                    continue
                if path == PurePosixPath("/"):
                    raise ValueError("invalid root replacement")
                paths.add(path.relative_to("/").as_posix())
        return tuple(sorted(paths))

    def _client(self):
        executable = self.executable if self.executable is not None else native_executable()
        if not executable.is_file():
            raise FileSystemUnavailable("未找到原生文件服务程序，请先安装与当前平台匹配的文件服务运行包。")
        from redpanda.filesystem.file_view import Client
        return Client(executable, self.storage, self.root)

    @staticmethod
    def _publish(client):
        from redpanda.filesystem.file_view import publication
        for transaction in publication.unfinished(client.store):
            result = publication.execute(client, transaction)
            if not result.get("finalized"):
                raise RuntimeError(f"file publication requires reconciliation: {result}")
        result = publication.publish(client)
        if result.get("status") != "no_pending_commands" and not result.get("finalized"):
            raise RuntimeError(f"file publication conflict: {result}")

    async def _publish_pending(self, client):
        async with _sqlite_lock(self.storage / "publication-lock.sqlite"):
            await settled(self._publish, client)

    async def execute(self, identity: str, callback):
        """Authorized operations retain actual effects, even for failed tool results."""
        validate_version(identity)
        if identity == INITIAL: raise ValueError("invalid file operation identity")
        async with self._owner():
            client = self._client()
            try:
                await self._publish_pending(client)
                begin = await settled(client.begin, identity)
                if begin["status"] == "existing_command":
                    begin = begin["receipt"]
                result_path = self.storage / "commands" / identity / "tool-result.json"
                if begin["status"] in ("sealed", "accepted"):
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if begin["status"] == "sealed":
                        accepted = await settled(lambda: client.request("accept", command_id=identity))
                        if accepted["status"] != "accepted": raise RuntimeError(accepted)
                    await self._publish_pending(client)
                    return result
                if begin["status"] != "active":
                    raise RuntimeError(f"file operation requires explicit recovery: {begin}")
                token = self._mount.set(Path(begin["mount"]))
                try:
                    result = await callback()
                finally:
                    self._mount.reset(token)
                files = await settled(lambda: client.request("finish"))
                if files["status"] != "sealed": raise RuntimeError(files)
                from .state import atomic
                await settled(atomic, result_path, result)
                accepted = await settled(lambda: client.request("accept", command_id=identity))
                if accepted["status"] != "accepted": raise RuntimeError(accepted)
                await self._publish_pending(client)
                return result
            finally:
                await settled(client.close)

    async def restore(self, version: str, *, identity: str, policy: str = "preserve") -> WorkspaceRestore:
        validate_version(version)
        validate_version(identity)
        if identity == INITIAL: raise ValueError("invalid file operation identity")
        if policy not in ("original", "preserve"): raise ValueError("invalid restoration policy")
        async with self._owner():
            noop = self.storage / "noops" / (identity + ".json")
            if noop.exists():
                from .state import fields, load
                saved = load(noop)
                fields(saved, ("target", "policy", "version"))
                validate_version(saved["target"])
                validate_version(saved["version"])
                if saved["target"] != version or saved["policy"] != policy:
                    raise ValueError("sandbox restoration identity was reused")
                return WorkspaceRestore(saved["version"], saved["version"])
            client = self._client()
            try:
                await self._publish_pending(client)
                history = await settled(self._history)
                existing = await settled(lambda: client.request("status", command_id=identity))
                if existing["status"] == "accepted":
                    history = history[:history.index(identity)]
                before = history[-1] if history else INITIAL
                if version == INITIAL:
                    targets = history
                else:
                    if version not in history: raise UnknownWorkspaceVersion(version)
                    targets = history[history.index(version)+1:]
                if not targets:
                    from .state import atomic
                    noop.parent.mkdir(exist_ok=True)
                    await settled(atomic, noop, {"target": version, "policy": policy, "version": before})
                    return WorkspaceRestore(before, before)
                result = await settled(lambda: client.restore(identity, targets, policy))
                if result["status"] == "conflict":
                    reasons = {
                        "structure_changed": "文件类型已变化",
                        "identity_changed": "文件已被替换",
                        "length_changed": "文件长度已变化，无法安全恢复",
                        "content_unavailable": "缺少恢复所需的原文",
                        "directory_contains_user_children": "目录包含需要保留的文件",
                        "unsupported_directory_replacement": "不支持恢复该目录替换",
                        "unsupported_directory_rename": "不支持恢复该目录重命名",
                        "parent_changed": "上级目录已变化",
                        "unsupported_link_restore": "不支持恢复该链接变化",
                        "unsupported_rename_cycle": "不支持恢复该组相互重命名",
                    }
                    path = result['path'].removeprefix('/')
                    raise WorkspaceRestoreFailed(before,
                        f"无法回退 {path}：{reasons[result['reason']]}。", (path,))
                if result["status"] not in ("sealed", "accepted"):
                    raise RuntimeError(f"sandbox restoration requires explicit recovery: {result}")
                accepted = await settled(lambda: client.request("accept", command_id=identity))
                if accepted["status"] != "accepted": raise RuntimeError(accepted)
                await self._publish_pending(client)
                summary = result["restore_summary"]
                return WorkspaceRestore(before, identity,
                    tuple(path.removeprefix("/") for path in summary["changed_paths"]),
                    tuple(path.removeprefix("/") for path in summary["preserved_paths"]),
                    tuple(change["path"].removeprefix("/") for change in result["receipt"]["changes"]
                          if change["after"]["kind"] == "missing"))
            finally:
                await settled(client.close)
