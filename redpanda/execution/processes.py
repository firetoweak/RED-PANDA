"""Host-owned processes; records outlive Session Workers, not the Host."""
from __future__ import annotations

import asyncio
import codecs
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .command import CommandStartError, strip_terminal_control
from .local.provider import create_host_command_runner


@dataclass
class OwnedProcess:
    session_id: str
    process_id: str
    command: str
    cwd: Path
    running: object
    log_dir: Path
    state: str = "running"
    exit_code: int | None = None

    def status(self) -> dict:
        return {"process_id": self.process_id, "pid": self.running.pid,
                "command": self.command, "cwd": str(self.cwd),
                "state": self.state, "exit_code": self.exit_code}


def read_output(path: Path, offset: int, *, finished: bool) -> dict:
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(16_384)
        at_end = not stream.read(1)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    content = decoder.decode(data, final=finished and at_end)
    pending, _ = decoder.getstate()
    next_offset = offset + len(data) - len(pending)
    return {"content": strip_terminal_control(content), "next_offset": next_offset,
            "has_more": not at_end}


class ProcessManager:
    def __init__(self, root: Path, environment):
        self.root = root
        self.environment = dict(environment)
        self._processes: dict[str, OwnedProcess] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._retired_sessions: set[str] = set()

    async def start(self, session_id: str, command: str, cwd: Path) -> dict:
        if sys.platform == "win32":
            from .process_win import Running
        elif sys.platform == "linux":
            from .process_linux import Running
        else:
            raise RuntimeError("Persistent processes require Windows or Linux")
        async with self._lock:
            if self._closed:
                raise RuntimeError("Process owner is closed")
            if session_id in self._retired_sessions:
                raise RuntimeError("Process Session is retired")
            runner = create_host_command_runner(self.environment)
            argv, environment = runner.prepare(command)
            process_id = uuid4().hex
            log_dir = self.root / process_id
            log_dir.mkdir(parents=True)
            spawn = asyncio.create_task(asyncio.to_thread(Running, argv, cwd, environment, log_dir))
            try:
                running = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                running = await spawn
                await asyncio.to_thread(running.close)
                raise
            except OSError as exc:
                raise CommandStartError(str(exc)) from exc
            entry = OwnedProcess(session_id, process_id, command, cwd, running, log_dir)
            self._processes[process_id] = entry
            return entry.status()

    async def _refresh(self, entry: OwnedProcess) -> None:
        if entry.state != "running":
            return
        poll = asyncio.create_task(asyncio.to_thread(entry.running.poll))
        try:
            code = await asyncio.shield(poll)
        except asyncio.CancelledError:
            await poll
            raise
        if code is not None:
            # The entire owned process tree is empty, including an exited Shell's descendants.
            cleanup = asyncio.create_task(asyncio.to_thread(entry.running.close))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                entry.exit_code, entry.state = code, "exited"
                raise
            entry.exit_code, entry.state = code, "exited"

    async def read(self, session_id: str, process_id: str, stdout_offset: int, stderr_offset: int) -> dict | None:
        async with self._lock:
            entry = self._processes.get(process_id)
            if entry is None or entry.session_id != session_id:
                return None
            await self._refresh(entry)
            return {**entry.status(), **{
                name: read_output(entry.log_dir / f"{name}.bin", offset, finished=entry.state != "running")
                for name, offset in (("stdout", stdout_offset), ("stderr", stderr_offset))
            }}

    async def _stop(self, entry: OwnedProcess) -> dict:
        await self._refresh(entry)
        if entry.state == "running":
            cleanup = asyncio.create_task(asyncio.to_thread(entry.running.close))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                entry.state = "stopped"
                raise
            entry.state = "stopped"
        return entry.status()

    async def stop(self, session_id: str, process_id: str) -> dict | None:
        async with self._lock:
            entry = self._processes.get(process_id)
            if entry is None or entry.session_id != session_id:
                return None
            return await self._stop(entry)

    async def list(self, session_id: str) -> list[dict]:
        async with self._lock:
            entries = [entry for entry in self._processes.values() if entry.session_id == session_id]
            for entry in entries:
                await self._refresh(entry)
            return [entry.status() for entry in entries]

    async def stop_session(self, session_id: str) -> None:
        async with self._lock:
            self._retired_sessions.add(session_id)
            for entry in self._processes.values():
                if entry.session_id == session_id:
                    await self._stop(entry)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            for entry in self._processes.values():
                await self._stop(entry)
