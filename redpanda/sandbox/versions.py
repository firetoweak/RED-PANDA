"""Operation references and native projections; no workspace snapshots or Git rollback."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3

INITIAL = "initial"

def native_executable() -> Path:
    configured = os.environ.get("REDPANDA_SANDBOX_EXECUTABLE")
    return Path(configured) if configured is not None else Path(__file__).with_name("bin") / "redpanda-sandbox.exe"

def validate_version(version: str) -> None:
    if type(version) is not str or (version != INITIAL and re.fullmatch(r"[0-9a-f]{64}", version) is None):
        raise ValueError("invalid sandbox operation reference")

def operation_id(session_id: str, command_id: str) -> str:
    return sha256(json.dumps([session_id, command_id], separators=(",", ":")).encode()).hexdigest()

class UnknownWorkspaceVersion(ValueError):
    pass

class SandboxUnavailable(OSError):
    """The native sandbox program is unavailable."""

class WorkspaceRestoreFailed(OSError):
    def __init__(self, before_version: str, error: str) -> None:
        super().__init__(error)
        self.before_version = before_version

@dataclass(frozen=True)
class WorkspaceRestore:
    before_version: str
    version: str

async def settled(function, *args):
    """A native lifecycle must settle before cancellation releases ownership."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            await task
        except BaseException as error:
            raise BaseExceptionGroup("sandbox operation failed during cancellation", [cancelled, error]) from None
        raise

class WorkspaceVersions:
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
            raise RuntimeError("task filesystem access outside a sandbox operation")
        return mount / logical.relative_to(self.root)

    def logical_path(self, native: Path) -> Path:
        mount = self._mount.get()
        if mount is None:
            raise RuntimeError("task filesystem access outside a sandbox operation")
        return self.root / native.relative_to(mount) if native.is_relative_to(mount) else native

    @asynccontextmanager
    async def _owner(self):
        async with self._serial:
            self.storage.mkdir(parents=True, exist_ok=True)
            def acquire():
                connection = sqlite3.connect(self.storage / "operation-lock.sqlite", timeout=300, check_same_thread=False)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                except BaseException:
                    connection.close()
                    raise
                return connection
            acquisition = asyncio.create_task(asyncio.to_thread(acquire))
            try:
                connection = await asyncio.shield(acquisition)
            except asyncio.CancelledError:
                connection = await acquisition
                await settled(connection.close)
                raise
            try:
                yield
            finally:
                await settled(connection.close)

    def _history(self):
        head = self.storage / "HEAD"
        if not head.exists(): return []
        from redpanda.sandbox.file_view.publication import fields, load, command_id
        import uuid
        next_commit = head.read_text(encoding="utf-8")
        operations, seen = [], set()
        while next_commit is not None:
            if str(uuid.UUID(next_commit)) != next_commit or next_commit in seen:
                raise ValueError("invalid sandbox commit chain")
            seen.add(next_commit)
            commit = load(self.storage / "commits" / (next_commit + ".json"))
            fields(commit, ("previous", "digest", "command_id", "kind"))
            if commit["kind"] == "accept":
                command_id(commit["command_id"])
                validate_version(commit["command_id"])
                if commit["command_id"] == INITIAL or type(commit["digest"]) is not str or re.fullmatch(r"[0-9a-f]{64}", commit["digest"]) is None:
                    raise ValueError("invalid accepted sandbox commit")
                operations.append(commit["command_id"])
            elif commit["kind"] != "rebase" or commit["digest"] is not None or commit["command_id"] is not None:
                raise ValueError("invalid sandbox commit")
            next_commit = commit["previous"]
        return list(reversed(operations))

    async def record(self) -> str:
        async with self._owner():
            history = await settled(self._history)
            return history[-1] if history else INITIAL

    def _client(self):
        executable = self.executable if self.executable is not None else native_executable()
        if not executable.is_file():
            raise SandboxUnavailable("运行 scripts/build_sandbox.py 构建本仓库的原生 sandbox，或配置 REDPANDA_SANDBOX_EXECUTABLE")
        from redpanda.sandbox.file_view import Client
        return Client(executable, self.storage, self.root)

    @staticmethod
    def _publish(client):
        from redpanda.sandbox.file_view import publication
        for transaction in publication.unfinished(client.store):
            result = publication.execute(client, transaction)
            if not result.get("finalized"):
                raise RuntimeError(f"sandbox publication requires reconciliation: {result}")
        result = publication.publish(client)
        if result.get("status") != "no_pending_commands" and not result.get("finalized"):
            raise RuntimeError(f"sandbox publication conflict: {result}")

    async def execute(self, identity: str, callback):
        """Authorized operations retain actual effects, even for failed tool results."""
        validate_version(identity)
        if identity == INITIAL: raise ValueError("invalid sandbox operation identity")
        async with self._owner():
            client = self._client()
            try:
                await settled(self._publish, client)
                begin = await settled(client.begin, identity)
                if begin["status"] == "existing_command":
                    begin = begin["receipt"]
                result_path = self.storage / "commands" / identity / "tool-result.json"
                if begin["status"] in ("sealed", "accepted"):
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if begin["status"] == "sealed":
                        accepted = await settled(lambda: client.request("accept", command_id=identity))
                        if accepted["status"] != "accepted": raise RuntimeError(accepted)
                    await settled(self._publish, client)
                    return result
                if begin["status"] != "active":
                    raise RuntimeError(f"sandbox operation requires explicit recovery: {begin}")
                token = self._mount.set(Path(begin["mount"]))
                try:
                    result = await callback()
                finally:
                    self._mount.reset(token)
                files = await settled(lambda: client.request("finish"))
                if files["status"] != "sealed": raise RuntimeError(files)
                from redpanda.sandbox.file_view.publication import atomic
                await settled(atomic, result_path, result)
                accepted = await settled(lambda: client.request("accept", command_id=identity))
                if accepted["status"] != "accepted": raise RuntimeError(accepted)
                await settled(self._publish, client)
                return result
            finally:
                await settled(client.close)

    async def restore(self, version: str, *, identity: str, policy: str = "preserve") -> WorkspaceRestore:
        validate_version(version)
        validate_version(identity)
        if identity == INITIAL: raise ValueError("invalid sandbox operation identity")
        if policy not in ("original", "preserve"): raise ValueError("invalid restoration policy")
        async with self._owner():
            noop = self.storage / "noops" / (identity + ".json")
            if noop.exists():
                from redpanda.sandbox.file_view.publication import fields, load
                saved = load(noop)
                fields(saved, ("target", "policy", "version"))
                validate_version(saved["target"])
                validate_version(saved["version"])
                if saved["target"] != version or saved["policy"] != policy:
                    raise ValueError("sandbox restoration identity was reused")
                return WorkspaceRestore(saved["version"], saved["version"])
            client = self._client()
            try:
                await settled(self._publish, client)
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
                    from redpanda.sandbox.file_view.publication import atomic
                    noop.parent.mkdir(exist_ok=True)
                    await settled(atomic, noop, {"target": version, "policy": policy, "version": before})
                    return WorkspaceRestore(before, before)
                result = await settled(lambda: client.restore(identity, targets, policy))
                if result["status"] == "conflict":
                    raise WorkspaceRestoreFailed(before, f"{result['reason']}: {result['path']}")
                if result["status"] not in ("sealed", "accepted"):
                    raise RuntimeError(f"sandbox restoration requires explicit recovery: {result}")
                accepted = await settled(lambda: client.request("accept", command_id=identity))
                if accepted["status"] != "accepted": raise RuntimeError(accepted)
                await settled(self._publish, client)
                return WorkspaceRestore(before, identity)
            finally:
                await settled(client.close)
