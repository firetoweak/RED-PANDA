from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from redpanda.assistant.runner import SessionNotFoundError
from redpanda.assistant.workspaces import workspace_binding, directory_binding
from redpanda.runtime import SqliteJournal
from redpanda.runtime.events import (
    DeliveryIdentity,
    DomainFactCommitted,
    EventDraft,
)


class SessionStore:
    """Identity to directory mapping.

    Workers open existing Journals. Host may open one only when that Session
    has no Worker, to persist a parent-initiated return.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        if type(session_id) is not str or not session_id:
            raise ValueError("Session identity must be a nonempty string")
        key = sha256(session_id.encode("utf-8")).hexdigest()
        return self.root / key / "journal.sqlite"

    def require(self, session_id: str) -> Path:
        path = self.path(session_id)
        if not path.parent.exists():
            raise SessionNotFoundError(session_id)
        if not path.is_file():
            raise ValueError(f"Session Journal missing: {path}")
        return path

    def journals(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for entry in self.root.iterdir():
            if not entry.is_dir() or len(entry.name) != 64 or any(
                char not in "0123456789abcdef" for char in entry.name
            ):
                continue
            journal = entry / "journal.sqlite"
            if not journal.is_file():
                raise ValueError(f"Session Journal missing: {journal}")
            paths.append(journal)
        return tuple(sorted(paths))

    async def create(
        self,
        session_id: str,
        *,
        workspace_id: str | None = None,
        directory: Path | None = None,
        initial_fact: dict | None = None,
        additional_facts: tuple[dict, ...] = (),
        agent_colleagues: tuple[str, ...] | None = None,
    ) -> None:
        if workspace_id is not None and directory is not None:
            raise ValueError("Session binds either a project or a directory")
        path = self.path(session_id)
        if path.parent.exists():
            raise ValueError(f"Session 已存在: {session_id}")
        staging = self.root / f".creating-{uuid4().hex}"
        staging.mkdir()
        journal = SqliteJournal(staging / "journal.sqlite")
        await journal.create_session(session_id)
        if workspace_id is not None or directory is not None:
            await journal.accept_delivery(
                EventDraft(
                    event_id=f"event_{uuid4().hex}",
                    session_id=session_id,
                    payload=directory_binding(directory) if directory is not None else workspace_binding(workspace_id),
                    occurred_at=datetime.now(timezone.utc),
                    delivery=DeliveryIdentity("workspace", uuid4().hex),
                )
            )
        facts = (() if initial_fact is None else (initial_fact,)) + additional_facts
        for initial_fact in facts:
            await journal.accept_delivery(
                EventDraft(
                    event_id=f"event_{uuid4().hex}",
                    session_id=session_id,
                    payload=DomainFactCommitted(
                        initial_fact["fact_type"],
                        initial_fact["data"],
                        requests_decision=initial_fact["requests_decision"],
                    ),
                    occurred_at=datetime.now(timezone.utc),
                    delivery=DeliveryIdentity(
                        initial_fact["source"], initial_fact["delivery_id"]
                    ),
                )
            )
        if agent_colleagues is not None:
            (staging / "agent-creation.json").write_text(json.dumps({"colleagues": agent_colleagues}), encoding="utf-8")
        os.rename(staging, path.parent)
