"""Host-owned commands. Journal records handoffs; exit files record process results."""
from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from .command import BoundedTextCapture, CaptureLimit, CommandStartError, strip_terminal_control
from .local.provider import create_host_command_runner


def command_identity(session_id: str, command_id: str) -> str:
    return hashlib.sha256(f"{session_id}/{command_id}".encode()).hexdigest()


def read_output(path: Path, offset: int, *, finished: bool) -> dict:
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(16_384)
        at_end = not stream.read(1)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    content = decoder.decode(data, final=finished and at_end)
    pending, _ = decoder.getstate()
    return {"content": strip_terminal_control(content), "next_offset": offset + len(data) - len(pending),
            "has_more": not at_end}


def _capture(path: Path, limit: CaptureLimit) -> dict:
    capture = BoundedTextCapture(limit)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with path.open("rb") as stream:
        # A running command may keep writing; capture only this snapshot.
        remaining = os.fstat(stream.fileno()).st_size
        while remaining:
            data = stream.read(min(16_384, remaining))
            remaining -= len(data)
            capture.feed(decoder.decode(data, final=remaining == 0))
    return capture.finish().to_dict()


@dataclass
class OwnedCommand:
    session_id: str
    command_id: str
    command: str
    cwd: str
    started_at: str
    log_dir: Path
    running: object | None = None
    pid: int | None = None
    state: str = "running"
    exit_code: int | None = None
    ended_at: str | None = None
    backgrounded: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    finished: asyncio.Future | None = None
    monitor: asyncio.Task | None = None

    def status(self) -> dict:
        ended = datetime.now(timezone.utc) if self.ended_at is None else datetime.fromisoformat(self.ended_at)
        return {"command_id": self.command_id, "pid": self.pid, "command": self.command, "cwd": self.cwd,
                "started_at": self.started_at, "state": self.state, "exit_code": self.exit_code,
                "duration_ms": round((ended - datetime.fromisoformat(self.started_at)).total_seconds() * 1000)}


class CommandManager:
    def __init__(self, root: Path, environment):
        self.root, self.environment = root, dict(environment)
        self._commands: dict[str, OwnedCommand] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._interrupts: set[str] = set()

    async def execute(self, session_id: str, command_id: str, command: str, cwd: Path,
                      wait_seconds: int, *, environment_overlay=None) -> dict:
        if sys.platform == "win32":
            from .process_win import Running
        elif sys.platform in {"linux", "darwin"}:
            from .process_owner import Running
        else:
            raise RuntimeError("Background commands require Windows, Linux or macOS")
        # Recovery queries share this lock; an in-flight spawn is never "not started".
        async with self._lock:
            if self._closed:
                raise RuntimeError("Command owner is closed")
            if command_id in self._commands:
                raise ValueError("command already started")
            overlay = environment_overlay or {}
            environment = {key: value for key, value in self.environment.items() if key.upper() not in overlay}
            runner = create_host_command_runner(environment | overlay)
            argv, environment = runner.prepare(command)
            directory = self.root / command_id
            directory.mkdir(parents=True)
            entry = OwnedCommand(session_id, command_id, command, str(cwd),
                                 datetime.now(timezone.utc).isoformat(), directory,
                                 finished=asyncio.get_running_loop().create_future())
            if command_id in self._interrupts:
                for name in ("stdout", "stderr"):
                    (directory / f"{name}.bin").touch()
                self._commands[command_id] = entry
                await self._finish(entry, "stopped", None)
                return await self.result(entry)
            spawn = asyncio.create_task(asyncio.to_thread(Running, argv, cwd, environment, directory))
            cancelled = False
            try:
                running = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                running = await spawn
                cancelled = True
            except OSError as exc:
                raise CommandStartError(str(exc)) from exc
            entry.running, entry.pid = running, running.pid
            self._commands[command_id] = entry
            entry.monitor = asyncio.create_task(self._monitor(entry))
            if cancelled:
                raise asyncio.CancelledError
        try:
            await asyncio.wait_for(asyncio.shield(entry.finished), timeout=wait_seconds)
        except TimeoutError:
            entry.backgrounded = True
        return await self.result(entry, limit=CaptureLimit(max_chars=4000, head_chars=0)
                                 if entry.backgrounded else CaptureLimit())

    async def _monitor(self, entry: OwnedCommand) -> None:
        try:
            while entry.state == "running":
                async with entry.lock:
                    if entry.state == "running":
                        code = await asyncio.to_thread(entry.running.poll)
                        if code is not None:
                            await self._finish(entry, "completed", code)
                if entry.state == "running":
                    await asyncio.sleep(.05)
        except BaseException as error:
            if not entry.finished.done():
                entry.finished.set_exception(error)
            raise

    async def _finish(self, entry: OwnedCommand, state: str, code: int | None) -> None:
        if entry.running is not None:
            await asyncio.to_thread(entry.running.close)
        entry.state, entry.exit_code = state, code
        entry.ended_at = datetime.now(timezone.utc).isoformat()
        self._save_exit(entry)
        entry.finished.set_result(None)

    def _save_exit(self, entry: OwnedCommand) -> None:
        entry.log_dir.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=entry.log_dir, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump({"version": 1, "session_id": entry.session_id, "ended_at": entry.ended_at,
                           **entry.status()}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                temporary.unlink()
                raise
        try:
            os.replace(temporary, entry.log_dir / "exit.json")
        finally:
            temporary.unlink(missing_ok=True)

    async def result(self, entry: OwnedCommand, *, limit: CaptureLimit = CaptureLimit()) -> dict:
        status = {**entry.status(), "backgrounded": entry.backgrounded}
        output = await asyncio.to_thread(lambda: {
            name: _capture(entry.log_dir / f"{name}.bin", limit) for name in ("stdout", "stderr")})
        return {**status, **output}

    async def lookup(self, session_id: str, command_id: str) -> dict | None:
        async with self._lock:
            entry = self._commands.get(command_id)
            if entry is None or entry.session_id != session_id:
                return None
            return await self.result(entry)

    async def restore(self, session_id: str, reference: dict) -> dict:
        identity = reference["command_id"]
        async with self._lock:
            entry = self._commands.get(identity)
            if entry is not None:
                if entry.session_id != session_id:
                    raise ValueError("command belongs to another session")
                return await self.result(entry)
            directory = self.root / identity
            exit_path = directory / "exit.json"
            if exit_path.exists():
                data = json.loads(exit_path.read_text(encoding="utf-8"))
                if data["version"] != 1 or data["session_id"] != session_id or data["command_id"] != identity:
                    raise ValueError("invalid command exit record")
                state, code, ended_at = data["state"], data["exit_code"], data["ended_at"]
                if (state not in {"completed", "stopped", "unknown"}
                    or (state == "completed" and type(code) is not int)
                    or (state != "completed" and code is not None)
                    or data["command"] != reference["command"] or data["cwd"] != reference["cwd"]
                    or data["started_at"] != reference["started_at"] or data["pid"] != reference["pid"]
                    or type(ended_at) is not str or datetime.fromisoformat(ended_at).tzinfo is None):
                    raise ValueError("invalid command exit result")
            else:
                state, code, ended_at = "unknown", None, datetime.now(timezone.utc).isoformat()
            entry = OwnedCommand(session_id, identity, reference["command"], reference["cwd"],
                                 reference["started_at"], directory, pid=reference["pid"], state=state,
                                 exit_code=code, ended_at=ended_at, backgrounded=True)
            self._commands[identity] = entry
            if not exit_path.exists():
                self._save_exit(entry)
            return await self.result(entry)

    async def wait(self, session_id: str, command_id: str) -> dict:
        entry = self._commands[command_id]
        if entry.session_id != session_id:
            raise ValueError("command belongs to another session")
        if entry.finished is not None:
            await asyncio.shield(entry.finished)
        return await self.result(entry, limit=CaptureLimit(max_chars=4000, head_chars=0))

    def background(self, session_id: str, command_id: str) -> None:
        entry = self._commands[command_id]
        if entry.session_id != session_id:
            raise ValueError("command belongs to another session")
        entry.backgrounded = True

    async def read(self, session_id: str, command_id: str, stdout_offset: int, stderr_offset: int) -> dict | None:
        async with self._lock:
            entry = self._commands.get(command_id)
            if entry is None or entry.session_id != session_id:
                return None
            return {**entry.status(), **{
                name: read_output(entry.log_dir / f"{name}.bin", offset, finished=entry.state != "running")
                for name, offset in (("stdout", stdout_offset), ("stderr", stderr_offset))}}

    async def stop(self, session_id: str, command_id: str) -> dict | None:
        async with self._lock:
            entry = self._commands.get(command_id)
        if entry is None or entry.session_id != session_id:
            return None
        async with entry.lock:
            if entry.state == "running":
                code = await asyncio.to_thread(entry.running.poll)
                await self._finish(entry, "stopped" if code is None else "completed", code)
            return entry.status()

    async def interrupt(self, session_id: str, command_id: str) -> None:
        # Unlike public stop, a foreground interrupt can arrive before spawn.
        async with self._lock:
            self._interrupts.add(command_id)
        await self.stop(session_id, command_id)

    async def list(self, session_id: str) -> list[dict]:
        return [entry.status() for entry in self._commands.values()
                if entry.session_id == session_id and entry.backgrounded]

    async def stop_session(self, session_id: str) -> None:
        for entry in tuple(self._commands.values()):
            if entry.session_id == session_id:
                await self.stop(session_id, entry.command_id)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
        for entry in tuple(self._commands.values()):
            await self.stop(entry.session_id, entry.command_id)
        await asyncio.gather(*(entry.monitor for entry in self._commands.values() if entry.monitor is not None))
