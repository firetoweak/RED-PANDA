from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from redpanda.assistant.runner import SessionNotFoundError
from redpanda.assistant.workspaces import workspace_binding
from redpanda.runtime import RuntimeStatus, SqliteJournal, replay
from redpanda.runtime.events import (
    DeliveryIdentity,
    DomainFactCommitted,
    EventDraft,
    UserMessageReceived,
)


@dataclass(frozen=True, slots=True)
class ForkedMessage:
    content: str
    artifact_refs: tuple[str, ...]


class ForkMessageNotFoundError(LookupError):
    pass


class SessionForkUnavailableError(ValueError):
    pass


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
        workspace_id: str,
        initial_fact: dict | None = None,
    ) -> None:
        path = self.path(session_id)
        if path.parent.exists():
            raise ValueError(f"Session 已存在: {session_id}")
        staging = self.root / f".creating-{uuid4().hex}"
        staging.mkdir()
        journal = SqliteJournal(staging / "journal.sqlite")
        await journal.create_session(session_id)
        await journal.accept_delivery(
            EventDraft(
                event_id=f"event_{uuid4().hex}",
                session_id=session_id,
                payload=workspace_binding(workspace_id),
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity("workspace", uuid4().hex),
            )
        )
        if initial_fact is not None:
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
        os.rename(staging, path.parent)

    async def fork_before_message(
        self,
        source_session_id: str,
        message_id: str,
        child_session_id: str,
    ) -> ForkedMessage:
        source_path = self.require(source_session_id)
        child_path = self.path(child_session_id)
        if child_path.parent.exists():
            raise ValueError(f"Session 已存在: {child_session_id}")

        source_journal = SqliteJournal(source_path)
        source_events = await source_journal.snapshot(source_session_id)
        target = next(
            (event for event in source_events if event.event_id == message_id),
            None,
        )
        if target is None:
            raise ForkMessageNotFoundError(message_id)
        if not isinstance(target.payload, UserMessageReceived):
            raise SessionForkUnavailableError(
                "fork target must be a user message"
            )
        prefix = tuple(
            event for event in source_events if event.sequence < target.sequence
        )
        state = replay(source_session_id, prefix).state
        if state.status is not RuntimeStatus.WAITING or state.waiting_for != (
            "external_fact",
        ):
            raise SessionForkUnavailableError(
                "fork prefix must end at an external-fact boundary"
            )
        await self._materialize(source_journal, prefix, child_session_id, child_path)
        return ForkedMessage(target.payload.content, target.artifact_refs)

    async def fork_after_turn(
        self,
        source_session_id: str,
        user_message_id: str,
        child_session_id: str,
    ) -> None:
        """从这条用户消息所在轮次的收口处切分支，含这一轮本身。

        前缀停在下一轮用户消息之前；这是最后一轮则含整本 Journal。
        一轮必须已经回到 WAITING(external_fact)，否则不是收口。
        """
        source_path = self.require(source_session_id)
        child_path = self.path(child_session_id)
        if child_path.parent.exists():
            raise ValueError(f"Session 已存在: {child_session_id}")

        source_journal = SqliteJournal(source_path)
        source_events = await source_journal.snapshot(source_session_id)
        target = next(
            (event for event in source_events if event.event_id == user_message_id),
            None,
        )
        if target is None:
            raise ForkMessageNotFoundError(user_message_id)
        if not isinstance(target.payload, UserMessageReceived):
            raise SessionForkUnavailableError(
                "branch target must be a user message"
            )
        next_user = next(
            (
                event
                for event in source_events
                if event.sequence > target.sequence
                and isinstance(event.payload, UserMessageReceived)
            ),
            None,
        )
        prefix = (
            tuple(
                event
                for event in source_events
                if event.sequence < next_user.sequence
            )
            if next_user is not None
            else source_events
        )
        state = replay(source_session_id, prefix).state
        if state.status is not RuntimeStatus.WAITING or state.waiting_for != (
            "external_fact",
        ):
            raise SessionForkUnavailableError(
                "turn must end at an external-fact boundary"
            )
        await self._materialize(source_journal, prefix, child_session_id, child_path)

    async def fork_after_event(
        self,
        source_session_id: str,
        boundary_event_id: str,
        child_session_id: str,
    ) -> None:
        """在某条事件之后切一条分支，含这条事件本身。

        不要求前缀停在外部事实边界。从 Step 边界重开时前缀是 RUNNABLE，
        这正是想要的：新身份带着完整历史停在那一刻，等人给下一句话。挡住
        自动续步的是暂停，不是这里的边界形状。
        """
        source_path = self.require(source_session_id)
        child_path = self.path(child_session_id)
        if child_path.parent.exists():
            raise ValueError(f"Session 已存在: {child_session_id}")

        source_journal = SqliteJournal(source_path)
        source_events = await source_journal.snapshot(source_session_id)
        target = next(
            (event for event in source_events if event.event_id == boundary_event_id),
            None,
        )
        if target is None:
            raise ForkMessageNotFoundError(boundary_event_id)
        prefix = tuple(
            event for event in source_events if event.sequence <= target.sequence
        )
        if replay(source_session_id, prefix).state.waiting_command_ids:
            raise SessionForkUnavailableError(
                "branch prefix has unresolved commands"
            )
        await self._materialize(source_journal, prefix, child_session_id, child_path)

    async def _materialize(self, source_journal, prefix, child_session_id, child_path):
        staging = self.root / f".creating-{uuid4().hex}"
        staging.mkdir()
        try:
            source_path = Path(source_journal.path)
            spans = await source_journal.history_through(
                prefix[-1].sequence if prefix else 0
            )
            asset_sources = tuple(
                path.relative_to(self.root).as_posix()
                for path in (*SqliteJournal.prefix_paths_at(source_path), source_path)
            )
            await SqliteJournal(staging / "journal.sqlite").create_branch(
                child_session_id, spans, asset_sources
            )
            os.rename(staging, child_path.parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
