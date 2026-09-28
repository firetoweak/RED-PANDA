from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from helperme.runtime.codec import (
    EVENT_SCHEMA_VERSION,
    STATE_CODEC_VERSION,
    STATE_PROJECTION_VERSION,
    decode_payload,
    decode_state,
    delivery_fingerprint,
    encode_payload,
    encode_state,
)
from helperme.runtime.events import (
    CommandAuthorized,
    CommandOutcomeReceived,
    CommandRejected,
    DecisionCancelled,
    DeliveryIdentity,
    DispatchAttemptStarted,
    DomainFactCommitted,
    Event,
    EventDraft,
    StepContinuationCancelled,
    StepCommitted,
    UserMessageReceived,
)
from helperme.runtime.journal.api import (
    AppendResult,
    AttemptTerminalConflict,
    DeliveryConflictError,
    LeaseLostError,
    StepClaimRequest,
    StepLease,
)
from helperme.runtime.model import (
    CanonicalState,
)


_T = TypeVar("_T")
SCHEMA_VERSION = 6
_READ_BATCH = 256


def _validate_journal_path(journal: str) -> None:
    path = Path(journal)
    if (
        path.is_absolute() or len(path.parts) != 2
        or len(path.parts[0]) != 64
        or any(char not in "0123456789abcdef" for char in path.parts[0])
        or path.name != "journal.sqlite"
    ):
        raise ValueError("history source Journal path is invalid")


@dataclass(frozen=True, slots=True)
class HistorySpan:
    journal: str
    session_id: str
    first_sequence: int
    last_sequence: int

    def __post_init__(self) -> None:
        _validate_journal_path(self.journal)
        if type(self.session_id) is not str or not self.session_id:
            raise ValueError("history span owner is invalid")
        if (
            type(self.first_sequence) is not int
            or type(self.last_sequence) is not int
            or self.first_sequence < 1
            or self.last_sequence < self.first_sequence
        ):
            raise ValueError("history span range is invalid")


async def _await_task_uninterruptibly(task: asyncio.Task[_T]) -> _T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    causation_id TEXT,
    correlation_id TEXT,
    artifact_refs_json TEXT NOT NULL,
    delivery_source TEXT,
    delivery_id TEXT,
    delivery_fingerprint TEXT,
    inherited INTEGER NOT NULL DEFAULT 0 CHECK (inherited IN (0, 1)),
    UNIQUE (session_id, sequence),
    CHECK (
        (delivery_source IS NULL
            AND delivery_id IS NULL
            AND delivery_fingerprint IS NULL)
        OR
        (delivery_source IS NOT NULL
            AND delivery_id IS NOT NULL
            AND delivery_fingerprint IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS history_spans (
    position INTEGER PRIMARY KEY CHECK (position >= 0),
    journal TEXT NOT NULL,
    session_id TEXT NOT NULL,
    first_sequence INTEGER NOT NULL CHECK (first_sequence >= 1),
    last_sequence INTEGER NOT NULL CHECK (last_sequence >= first_sequence)
);

CREATE TABLE IF NOT EXISTS asset_sources (
    journal TEXT PRIMARY KEY
);

CREATE UNIQUE INDEX IF NOT EXISTS events_delivery_identity
ON events(delivery_source, delivery_id)
WHERE delivery_source IS NOT NULL;

CREATE TABLE IF NOT EXISTS step_claims (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    trigger_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    decision_cursor INTEGER NOT NULL,
    basis_state_version TEXT NOT NULL,
    observed_journal_position INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_consumptions (
    trigger_event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    result_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    step_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS cancelled_step_continuations (
    step_event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    cancellation_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id)
);

CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    issued_event_id TEXT NOT NULL REFERENCES events(event_id),
    dispatch_eligible_event_id TEXT REFERENCES events(event_id),
    authorization_rejected_event_id TEXT UNIQUE REFERENCES events(event_id),
    canonical_outcome_event_id TEXT UNIQUE REFERENCES events(event_id),
    CHECK (
        dispatch_eligible_event_id IS NULL
        OR authorization_rejected_event_id IS NULL
    )
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL REFERENCES commands(command_id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    dispatch_event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
    claim_token TEXT NOT NULL UNIQUE,
    claim_expires_at REAL NOT NULL,
    worker_id TEXT NOT NULL,
    terminal_event_id TEXT UNIQUE REFERENCES events(event_id),
    UNIQUE (command_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    journal_position INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    codec_version INTEGER NOT NULL,
    projection_version TEXT NOT NULL,
    state_json TEXT NOT NULL
);

PRAGMA user_version = 6;
"""


class SqliteJournal:
    """Durable event Journal; each Session Worker owns its database file."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if str(path) == ":memory:":
            raise ValueError("SqliteJournal requires a durable file path")
        self._path = str(Path(path).resolve())
        self._clock = clock
        self._busy_timeout_seconds = busy_timeout_seconds
        self._initialize()
        self._spans, self._identity = self._read_history_header()
        self._prefix_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._prefix_events: tuple[Event, ...] | None = None
        self._prefix_by_id: dict[str, Event] = {}
        self._prefix_deliveries: dict[DeliveryIdentity, Event] = {}
        self._prefix_consumed: set[str] = set()
        self._prefix_cancelled_continuations: set[str] = set()
        self._snapshot_cache: tuple[Event, ...] | None = None

    @property
    def path(self) -> str:
        return self._path

    async def prepare_recovery(self, session_id: str) -> None:
        """新 Worker 独占接管时释放旧 Step claim；保留全部执行事实。"""

        def recover(connection: sqlite3.Connection) -> None:
            identities = connection.execute(
                "SELECT session_id FROM sessions"
            ).fetchall()
            if [row["session_id"] for row in identities] != [session_id]:
                raise ValueError("Session Journal identity mismatch")
            connection.execute(
                "UPDATE step_claims SET expires_at = 0 WHERE session_id = ?",
                (session_id,),
            )

        await self._write(recover)

    async def create_session(self, session_id: str) -> bool:
        self._validate_session_id(session_id)

        def create(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO sessions(session_id, last_sequence)
                VALUES (?, 0)
                """,
                (session_id,),
            )
            return cursor.rowcount == 1

        created = await self._write(create)
        if created:
            self._identity = session_id
        return created

    async def create_branch(
        self,
        session_id: str,
        spans: tuple[HistorySpan, ...],
        asset_sources: tuple[str, ...] = (),
    ) -> None:
        self._validate_session_id(session_id)
        for journal in asset_sources:
            _validate_journal_path(journal)
        expected = 1
        for span in spans:
            if span.first_sequence != expected:
                raise ValueError("history spans must be contiguous")
            expected = span.last_sequence + 1

        def create(connection: sqlite3.Connection) -> None:
            if connection.execute("SELECT 1 FROM sessions").fetchone() is not None:
                raise ValueError("branch target Journal must be empty")
            connection.execute(
                "INSERT INTO sessions(session_id, last_sequence) VALUES (?, ?)",
                (session_id, expected - 1),
            )
            connection.executemany(
                """
                INSERT INTO history_spans(
                    position, journal, session_id, first_sequence, last_sequence
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (index, span.journal, span.session_id,
                     span.first_sequence, span.last_sequence)
                    for index, span in enumerate(spans)
                ),
            )
            connection.executemany(
                "INSERT INTO asset_sources(journal) VALUES (?)",
                ((journal,) for journal in dict.fromkeys(asset_sources)),
            )

        await self._write(create)
        self._spans = spans
        self._identity = session_id

    async def history_through(self, sequence: int) -> tuple[HistorySpan, ...]:
        if type(sequence) is not int or sequence < 0:
            raise ValueError("history cutoff must be non-negative")

        def read() -> tuple[HistorySpan, ...]:
            connection = self._connect()
            try:
                row = connection.execute("SELECT last_sequence FROM sessions").fetchone()
                if row is None or sequence > row["last_sequence"]:
                    raise ValueError("history cutoff exceeds Journal")
            finally:
                connection.close()
            selected: list[HistorySpan] = []
            for span in self._spans:
                if span.first_sequence > sequence:
                    break
                selected.append(replace(span, last_sequence=min(span.last_sequence, sequence)))
            inherited_end = selected[-1].last_sequence if selected else 0
            if sequence > inherited_end:
                relative = Path(self._path).relative_to(
                    Path(self._path).parent.parent
                ).as_posix()
                selected.append(
                    HistorySpan(relative, self._identity,
                                inherited_end + 1, sequence)
                )
            return tuple(selected)

        return await self._read(read)

    @staticmethod
    def prefix_paths_at(path: Path) -> tuple[Path, ...]:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise ValueError(f"unsupported database schema version: {version}")
            rows = connection.execute(
                "SELECT journal, session_id, first_sequence, last_sequence "
                "FROM history_spans ORDER BY position"
            ).fetchall()
            asset_rows = connection.execute(
                "SELECT journal FROM asset_sources ORDER BY journal"
            ).fetchall()
        root = path.resolve().parent.parent
        spans = tuple(HistorySpan(*row) for row in rows)
        assets = tuple(row[0] for row in asset_rows)
        for journal in assets:
            _validate_journal_path(journal)
        return tuple(dict.fromkeys(
            root / journal for journal in
            (*[span.journal for span in spans], *assets)
        ))

    async def session_exists(self, session_id: str) -> bool:
        self._validate_session_id(session_id)

        def exists() -> bool:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                return row is not None
            finally:
                connection.close()

        return await self._read(exists)

    async def session_identity(self) -> str:
        def read() -> str:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT session_id FROM sessions"
                ).fetchall()
            finally:
                connection.close()
            if len(rows) != 1:
                raise ValueError("Session Journal must contain one identity")
            return rows[0]["session_id"]

        return await self._read(read)

    async def append(self, draft: EventDraft) -> Event:
        self._validate_generic_append(draft)
        return (
            await self._write(lambda connection: self._append_tx(connection, draft))
        ).event

    async def accept_delivery(self, draft: EventDraft) -> AppendResult:
        self._validate_external_delivery(draft)
        if draft.delivery is None:
            raise ValueError("external event requires delivery identity")
        return await self._write(lambda connection: self._append_tx(connection, draft))

    async def snapshot(self, session_id: str) -> tuple[Event, ...]:
        return await self._read(lambda: self._snapshot_sync(session_id))

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if type(session_id) is not str:
            raise TypeError("session id must be str")
        if not session_id:
            raise ValueError("session id must not be empty")

    async def acquire_step(
        self,
        request: StepClaimRequest,
        *,
        token: str,
        owner_id: str,
        lease_seconds: float,
    ) -> StepLease | None:
        if not token or not owner_id:
            raise ValueError("step claim identity must not be empty")
        if lease_seconds <= 0:
            raise ValueError("step lease duration must be positive")
        operation = asyncio.create_task(
            self._write(
                lambda connection: self._acquire_step_tx(
                    connection,
                    request,
                    token,
                    owner_id,
                    lease_seconds,
                )
            )
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            lease = await _await_task_uninterruptibly(operation)
            if lease is not None:
                compensation = asyncio.create_task(
                    self._write(
                        lambda connection: self._release_step_tx(
                            connection,
                            lease,
                        )
                    )
                )
                await _await_task_uninterruptibly(compensation)
            raise

    async def release_step(self, lease: StepLease) -> None:
        await self._write(lambda connection: self._release_step_tx(connection, lease))

    async def renew_step(
        self,
        lease: StepLease,
        *,
        lease_seconds: float,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("step lease duration must be positive")
        return await self._write(
            lambda connection: self._renew_step_tx(
                connection,
                lease,
                lease_seconds,
            )
        )

    async def commit_step(
        self,
        lease: StepLease,
        draft: EventDraft,
    ) -> Event:
        self._validate_internal_draft(draft)
        return await self._write(
            lambda connection: self._commit_step_tx(
                connection,
                lease,
                draft,
            )
        )

    async def cancel_decision(
        self,
        draft: EventDraft,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        if not isinstance(draft.payload, DecisionCancelled):
            raise TypeError(type(draft.payload).__name__)
        return await self._write(
            lambda connection: self._cancel_decision_tx(connection, draft)
        )

    async def cancel_continuation(
        self,
        draft: EventDraft,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        if not isinstance(draft.payload, StepContinuationCancelled):
            raise TypeError(type(draft.payload).__name__)
        return await self._write(
            lambda connection: self._cancel_continuation_tx(connection, draft)
        )

    async def start_attempt(
        self,
        draft: EventDraft,
        *,
        lease_seconds: float = 30.0,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, DispatchAttemptStarted):
            raise TypeError(type(payload).__name__)
        if lease_seconds <= 0:
            raise ValueError("attempt lease duration must be positive")
        operation = asyncio.create_task(
            self._write(
                lambda connection: self._start_attempt_tx(
                    connection,
                    draft,
                    lease_seconds,
                )
            )
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            event = await _await_task_uninterruptibly(operation)
            if event is not None:
                compensation = asyncio.create_task(
                    self.release_attempt(
                        payload.attempt_id,
                        payload.claim_token,
                    )
                )
                await _await_task_uninterruptibly(compensation)
            raise

    async def grant_command(self, draft: EventDraft) -> Event | None:
        self._validate_internal_draft(draft)
        if not isinstance(draft.payload, CommandAuthorized):
            raise TypeError(type(draft.payload).__name__)
        return await self._write(
            lambda connection: self._grant_command_tx(connection, draft)
        )

    async def reject_command(self, draft: EventDraft) -> Event | None:
        self._validate_internal_draft(draft)
        if not isinstance(draft.payload, CommandRejected):
            raise TypeError(type(draft.payload).__name__)
        return await self._write(
            lambda connection: self._reject_command_tx(connection, draft)
        )

    async def renew_attempt(
        self,
        attempt_id: str,
        claim_token: str,
        *,
        lease_seconds: float,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("attempt lease duration must be positive")
        return await self._write(
            lambda connection: self._renew_attempt_tx(
                connection,
                attempt_id,
                claim_token,
                lease_seconds,
            )
        )

    async def release_attempt(
        self,
        attempt_id: str,
        claim_token: str,
    ) -> None:
        await self._write(
            lambda connection: connection.execute(
                """
            UPDATE attempts SET claim_expires_at = 0
            WHERE attempt_id = ? AND claim_token = ?
            """,
                (attempt_id, claim_token),
            )
        )

    async def record_attempt_fact(
        self,
        draft: EventDraft,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, CommandOutcomeReceived):
            raise TypeError(type(payload).__name__)
        if payload.attempt_id is None:
            raise ValueError("attempt fact requires attempt identity")
        return await self._write(
            lambda connection: self._record_attempt_fact_tx(
                connection,
                draft,
            )
        )

    async def load_checkpoint(
        self,
        session_id: str,
        journal_position: int,
        fingerprint: str,
    ) -> CanonicalState | None:
        return await self._read(
            lambda: self._load_checkpoint_sync(
                session_id,
                journal_position,
                fingerprint,
            )
        )

    async def save_checkpoint(
        self,
        state: CanonicalState,
        fingerprint: str,
    ) -> None:
        state_json = encode_state(state)
        await self._write(
            lambda connection: self._save_checkpoint_tx(
                connection,
                state,
                fingerprint,
                state_json,
            )
        )

    async def delete_checkpoint(self, session_id: str) -> None:
        await self._write(
            lambda connection: connection.execute(
                "DELETE FROM checkpoints WHERE session_id = ?",
                (session_id,),
            )
        )

    def _initialize(self) -> None:
        connection = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_seconds,
            isolation_level=None,
        )
        try:
            connection.execute(
                f"PRAGMA busy_timeout = {int(self._busy_timeout_seconds * 1000)}"
            )
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError(f"unsupported database schema version: {version}")
            if version == 0:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)
            elif connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise ValueError("Session Journal must use WAL mode")
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(
            f"PRAGMA busy_timeout = {int(self._busy_timeout_seconds * 1000)}"
        )
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _read_history_header(self) -> tuple[tuple[HistorySpan, ...], str | None]:
        connection = self._connect()
        try:
            identities = connection.execute(
                "SELECT session_id, last_sequence FROM sessions"
            ).fetchall()
            if len(identities) > 1:
                raise ValueError("Session Journal must contain at most one identity")
            rows = connection.execute(
                "SELECT * FROM history_spans ORDER BY position"
            ).fetchall()
        finally:
            connection.close()
        if rows and not identities:
            raise ValueError("history spans require a Session identity")
        spans = tuple(
            HistorySpan(row["journal"], row["session_id"],
                        row["first_sequence"], row["last_sequence"])
            for row in rows
        )
        expected = 1
        for position, span in enumerate(spans):
            if rows[position]["position"] != position or span.first_sequence != expected:
                raise ValueError("history spans must be contiguous")
            expected = span.last_sequence + 1
        if identities and expected - 1 > identities[0]["last_sequence"]:
            raise ValueError("history spans exceed Journal position")
        return spans, identities[0]["session_id"] if identities else None

    @staticmethod
    def _read_event_rows(
        connection: sqlite3.Connection,
        session_id: str,
        first_sequence: int,
        last_sequence: int,
    ):
        next_sequence = first_sequence
        while next_sequence <= last_sequence:
            rows = connection.execute(
                """
                SELECT * FROM events WHERE session_id = ? AND inherited = 0
                    AND sequence BETWEEN ? AND ? ORDER BY sequence LIMIT ?
                """,
                (session_id, next_sequence, last_sequence, _READ_BATCH),
            ).fetchall()
            if not rows:
                raise ValueError("history span has a sequence gap")
            for row in rows:
                if row["sequence"] != next_sequence:
                    raise ValueError("history span has a sequence gap")
                yield row
                next_sequence += 1

    def _load_prefix_sync(self) -> tuple[Event, ...]:
        with self._prefix_lock:
            if self._prefix_events is not None:
                return self._prefix_events
            events: list[Event] = []
            root = Path(self._path).parent.parent
            for span in self._spans:
                path = root / span.journal
                if not path.is_file():
                    raise ValueError(f"history source Journal missing: {path}")
                connection = sqlite3.connect(
                    path.as_uri() + "?mode=ro", uri=True,
                    timeout=self._busy_timeout_seconds,
                )
                connection.row_factory = sqlite3.Row
                try:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version != SCHEMA_VERSION:
                        raise ValueError(f"unsupported history source schema version: {version}")
                    for row in self._read_event_rows(
                        connection, span.session_id,
                        span.first_sequence, span.last_sequence,
                    ):
                        source = self._event_from_row(row)
                        events.append(replace(source, session_id=self._identity))
                finally:
                    connection.close()
            self._prefix_events = tuple(events)
            self._prefix_by_id = {event.event_id: event for event in events}
            if len(self._prefix_by_id) != len(events):
                raise ValueError("history prefix contains duplicate event identity")
            self._prefix_deliveries = {
                event.delivery: event for event in events if event.delivery is not None
            }
            if len(self._prefix_deliveries) != sum(
                event.delivery is not None for event in events
            ):
                raise ValueError("history prefix contains duplicate delivery identity")
            self._prefix_consumed = {
                event.payload.step.trigger_event_id
                for event in events if isinstance(event.payload, StepCommitted)
            } | {
                event.payload.trigger_event_id
                for event in events if isinstance(event.payload, DecisionCancelled)
            }
            self._prefix_cancelled_continuations = {
                event.payload.step_event_id
                for event in events if isinstance(event.payload, StepContinuationCancelled)
            }
            return self._prefix_events

    async def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        if self._spans:
            await self._read(self._load_prefix_sync)
        thread = asyncio.create_task(
            asyncio.to_thread(
                self._write_sync,
                operation,
            )
        )
        try:
            return await asyncio.shield(thread)
        except asyncio.CancelledError:
            await _await_task_uninterruptibly(thread)
            raise

    async def _read(self, operation: Callable[[], _T]) -> _T:
        thread = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(thread)
        except asyncio.CancelledError:
            await _await_task_uninterruptibly(thread)
            raise

    def _write_sync(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = operation(connection)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _snapshot_sync(self, session_id: str) -> tuple[Event, ...]:
        if self._identity is None:
            self._spans, self._identity = self._read_history_header()
        if self._identity != session_id:
            return ()
        prefix = self._load_prefix_sync()
        with self._snapshot_lock:
            if self._snapshot_cache is None:
                self._snapshot_cache = prefix
            position = self._snapshot_cache[-1].sequence if self._snapshot_cache else 0
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT last_sequence FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                last_sequence = row["last_sequence"]
                if last_sequence < position:
                    raise ValueError("Session Journal position moved backward")
                additions = tuple(
                    self._event_from_row(event_row)
                    for event_row in self._read_event_rows(
                        connection, session_id, position + 1, last_sequence
                    )
                )
            finally:
                connection.close()
            self._snapshot_cache += additions
            return self._snapshot_cache

    def _events_tx(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> tuple[Event, ...]:
        rows = connection.execute(
            """
            SELECT * FROM events
            WHERE session_id = ? AND inherited = 0
            ORDER BY sequence
            """,
            (session_id,),
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def _ensure_event_row_tx(
        self, connection: sqlite3.Connection, event_id: str
    ) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is not None:
            return row
        event = self._prefix_by_id.get(event_id)
        if event is None:
            return None
        kind, payload_json = encode_payload(event.payload)
        fingerprint = (
            delivery_fingerprint(EventDraft(
                event_id=event.event_id,
                session_id=event.session_id,
                payload=event.payload,
                occurred_at=event.occurred_at,
                causation_id=event.causation_id,
                correlation_id=event.correlation_id,
                schema_version=event.schema_version,
                artifact_refs=event.artifact_refs,
                delivery=event.delivery,
            )) if event.delivery is not None else None
        )
        connection.execute(
            """
            INSERT INTO events(
                event_id, session_id, sequence, event_type, schema_version,
                payload_json, occurred_at, causation_id, correlation_id,
                artifact_refs_json, delivery_source, delivery_id,
                delivery_fingerprint, inherited
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                event.event_id, event.session_id, event.sequence, kind,
                event.schema_version, payload_json, event.occurred_at.isoformat(),
                event.causation_id, event.correlation_id,
                self._json_dump(list(event.artifact_refs)),
                event.delivery.source if event.delivery is not None else None,
                event.delivery.delivery_id if event.delivery is not None else None,
                fingerprint,
            ),
        )
        return connection.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()

    def _append_tx(
        self,
        connection: sqlite3.Connection,
        draft: EventDraft,
        *,
        attempt_lease_expires_at: float | None = None,
    ) -> AppendResult:
        if draft.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported event schema version: {draft.schema_version}"
            )
        row = connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (draft.event_id,),
        ).fetchone()
        if row is not None:
            existing = self._event_from_row(row)
            if not self._same_event(existing, draft):
                raise ValueError(f"event id conflict: {draft.event_id}")
            return AppendResult(existing, False)
        inherited = self._prefix_by_id.get(draft.event_id)
        if inherited is not None:
            if not self._same_event(inherited, draft):
                raise ValueError(f"event id conflict: {draft.event_id}")
            return AppendResult(inherited, False)

        payload = draft.payload
        if (
            isinstance(payload, CommandOutcomeReceived)
            and payload.attempt_id is not None
        ):
            row = connection.execute(
                """
                SELECT events.* FROM attempts
                JOIN events ON events.event_id = attempts.terminal_event_id
                WHERE attempts.attempt_id = ?
                """,
                (payload.attempt_id,),
            ).fetchone()
            if row is not None:
                terminal = self._event_from_row(row)
                if terminal.payload != payload:
                    raise AttemptTerminalConflict(payload.attempt_id)
                return AppendResult(terminal, False)

        fingerprint: str | None = None
        if draft.delivery is not None:
            fingerprint = delivery_fingerprint(draft)
            row = connection.execute(
                """
                SELECT * FROM events
                WHERE delivery_source = ? AND delivery_id = ?
                """,
                (draft.delivery.source, draft.delivery.delivery_id),
            ).fetchone()
            if row is not None:
                if row["delivery_fingerprint"] != fingerprint:
                    raise DeliveryConflictError(
                        f"delivery content conflict: {draft.delivery}"
                    )
                return AppendResult(self._event_from_row(row), False)
            inherited_delivery = self._prefix_deliveries.get(draft.delivery)
            if inherited_delivery is not None:
                if not self._same_delivery(inherited_delivery, draft):
                    raise DeliveryConflictError(
                        f"delivery content conflict: {draft.delivery}"
                    )
                return AppendResult(inherited_delivery, False)

        if isinstance(payload, StepCommitted):
            self._ensure_event_row_tx(connection, payload.step.trigger_event_id)
        elif isinstance(payload, DecisionCancelled):
            self._ensure_event_row_tx(connection, payload.trigger_event_id)
        elif isinstance(payload, StepContinuationCancelled):
            self._ensure_event_row_tx(connection, payload.step_event_id)

        sequence = self._next_sequence(connection, draft.session_id)
        kind, payload_json = encode_payload(draft.payload)
        delivery_source = draft.delivery.source if draft.delivery is not None else None
        delivery_id = draft.delivery.delivery_id if draft.delivery is not None else None
        connection.execute(
            """
            INSERT INTO events(
                event_id,
                session_id,
                sequence,
                event_type,
                schema_version,
                payload_json,
                occurred_at,
                causation_id,
                correlation_id,
                artifact_refs_json,
                delivery_source,
                delivery_id,
                delivery_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.event_id,
                draft.session_id,
                sequence,
                kind,
                draft.schema_version,
                payload_json,
                draft.occurred_at.isoformat(),
                draft.causation_id,
                draft.correlation_id,
                self._json_dump(list(draft.artifact_refs)),
                delivery_source,
                delivery_id,
                fingerprint,
            ),
        )
        event = Event(
            event_id=draft.event_id,
            session_id=draft.session_id,
            sequence=sequence,
            payload=draft.payload,
            occurred_at=draft.occurred_at,
            causation_id=draft.causation_id,
            correlation_id=draft.correlation_id,
            schema_version=draft.schema_version,
            artifact_refs=draft.artifact_refs,
            delivery=draft.delivery,
        )
        self._index_event_tx(
            connection,
            event,
            attempt_lease_expires_at=attempt_lease_expires_at,
        )
        return AppendResult(event, True)

    @staticmethod
    def _next_sequence(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> int:
        row = connection.execute(
            "SELECT last_sequence FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO sessions(session_id, last_sequence) VALUES (?, 1)",
                (session_id,),
            )
            return 1
        sequence = row["last_sequence"] + 1
        connection.execute(
            "UPDATE sessions SET last_sequence = ? WHERE session_id = ?",
            (sequence, session_id),
        )
        return sequence

    def _acquire_step_tx(
        self,
        connection: sqlite3.Connection,
        request: StepClaimRequest,
        token: str,
        owner_id: str,
        lease_seconds: float,
    ) -> StepLease | None:
        trigger = self._ensure_event_row_tx(connection, request.trigger_event_id)
        if trigger is None or trigger["session_id"] != request.session_id:
            raise KeyError(request.trigger_event_id)
        if self._decision_consumed_tx(connection, request.trigger_event_id):
            return None

        current = connection.execute(
            "SELECT * FROM step_claims WHERE session_id = ?",
            (request.session_id,),
        ).fetchone()
        now = self._clock()
        if current is not None and current["expires_at"] > now:
            if current["token"] == token and self._request_from_row(current) == request:
                return self._lease_from_row(current)
            return None

        generation = current["generation"] + 1 if current is not None else 1
        expires_at = now + lease_seconds
        if current is None:
            connection.execute(
                """
                INSERT INTO step_claims(
                    session_id,
                    trigger_event_id,
                    decision_cursor,
                    basis_state_version,
                    observed_journal_position,
                    token,
                    owner_id,
                    generation,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.session_id,
                    request.trigger_event_id,
                    request.decision_cursor,
                    request.basis_state_version,
                    request.observed_journal_position,
                    token,
                    owner_id,
                    generation,
                    expires_at,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE step_claims SET
                    trigger_event_id = ?,
                    decision_cursor = ?,
                    basis_state_version = ?,
                    observed_journal_position = ?,
                    token = ?,
                    owner_id = ?,
                    generation = ?,
                    expires_at = ?
                WHERE session_id = ?
                """,
                (
                    request.trigger_event_id,
                    request.decision_cursor,
                    request.basis_state_version,
                    request.observed_journal_position,
                    token,
                    owner_id,
                    generation,
                    expires_at,
                    request.session_id,
                ),
            )
        return StepLease(
            request=request,
            token=token,
            owner_id=owner_id,
            generation=generation,
            expires_at=expires_at,
        )

    @staticmethod
    def _release_step_tx(
        connection: sqlite3.Connection,
        lease: StepLease,
    ) -> None:
        connection.execute(
            """
            DELETE FROM step_claims
            WHERE session_id = ?
                AND token = ?
                AND owner_id = ?
                AND generation = ?
            """,
            (
                lease.request.session_id,
                lease.token,
                lease.owner_id,
                lease.generation,
            ),
        )

    def _renew_step_tx(
        self,
        connection: sqlite3.Connection,
        lease: StepLease,
        lease_seconds: float,
    ) -> bool:
        now = self._clock()
        cursor = connection.execute(
            """
            UPDATE step_claims SET expires_at = ?
            WHERE session_id = ?
                AND trigger_event_id = ?
                AND decision_cursor = ?
                AND basis_state_version = ?
                AND observed_journal_position = ?
                AND token = ?
                AND owner_id = ?
                AND generation = ?
            """,
            (
                now + lease_seconds,
                lease.request.session_id,
                lease.request.trigger_event_id,
                lease.request.decision_cursor,
                lease.request.basis_state_version,
                lease.request.observed_journal_position,
                lease.token,
                lease.owner_id,
                lease.generation,
            ),
        )
        return cursor.rowcount == 1

    def _commit_step_tx(
        self,
        connection: sqlite3.Connection,
        lease: StepLease,
        draft: EventDraft,
    ) -> Event:
        payload = draft.payload
        if not isinstance(payload, StepCommitted):
            raise TypeError(type(payload).__name__)

        existing = connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (draft.event_id,),
        ).fetchone()
        if existing is not None:
            event = self._event_from_row(existing)
            if not self._same_event(event, draft):
                raise ValueError(f"event id conflict: {draft.event_id}")
            consumption = connection.execute(
                """
                SELECT result_event_id FROM decision_consumptions
                WHERE trigger_event_id = ?
                """,
                (lease.request.trigger_event_id,),
            ).fetchone()
            if consumption is None or consumption["result_event_id"] != event.event_id:
                raise LeaseLostError(lease.token)
            return event

        claim = connection.execute(
            "SELECT * FROM step_claims WHERE session_id = ?",
            (lease.request.session_id,),
        ).fetchone()
        if (
            claim is None
            or claim["token"] != lease.token
            or claim["owner_id"] != lease.owner_id
            or claim["generation"] != lease.generation
            or self._request_from_row(claim) != lease.request
        ):
            raise LeaseLostError(lease.token)
        self._validate_step_draft(lease.request, draft)
        if self._decision_consumed_tx(connection, lease.request.trigger_event_id):
            raise LeaseLostError(lease.token)

        event = self._append_tx(connection, draft).event
        connection.execute(
            """
            DELETE FROM step_claims
            WHERE session_id = ? AND token = ? AND generation = ?
            """,
            (
                lease.request.session_id,
                lease.token,
                lease.generation,
            ),
        )
        return event

    def _cancel_decision_tx(
        self,
        connection: sqlite3.Connection,
        draft: EventDraft,
    ) -> Event | None:
        payload = draft.payload
        if not isinstance(payload, DecisionCancelled):
            raise TypeError(type(payload).__name__)
        existing = connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (draft.event_id,),
        ).fetchone()
        if existing is not None:
            event = self._event_from_row(existing)
            if not self._same_event(event, draft):
                raise ValueError(f"event id conflict: {draft.event_id}")
            consumption = connection.execute(
                """
                SELECT result_event_id FROM decision_consumptions
                WHERE trigger_event_id = ?
                """,
                (payload.trigger_event_id,),
            ).fetchone()
            if consumption is None or consumption["result_event_id"] != event.event_id:
                return None
            return event
        if self._decision_consumed_tx(connection, payload.trigger_event_id):
            return None
        return self._append_tx(connection, draft).event

    def _cancel_continuation_tx(
        self,
        connection: sqlite3.Connection,
        draft: EventDraft,
    ) -> Event | None:
        payload = draft.payload
        if not isinstance(payload, StepContinuationCancelled):
            raise TypeError(type(payload).__name__)
        existing = connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (draft.event_id,),
        ).fetchone()
        if existing is not None:
            event = self._event_from_row(existing)
            if not self._same_event(event, draft):
                raise ValueError(f"event id conflict: {draft.event_id}")
            cancellation = connection.execute(
                """
                SELECT cancellation_event_id FROM cancelled_step_continuations
                WHERE step_event_id = ?
                """,
                (payload.step_event_id,),
            ).fetchone()
            if (
                cancellation is None
                or cancellation["cancellation_event_id"] != event.event_id
            ):
                return None
            return event
        if (
            connection.execute(
                "SELECT 1 FROM cancelled_step_continuations WHERE step_event_id = ?",
                (payload.step_event_id,),
            ).fetchone()
            is not None
            or payload.step_event_id in self._prefix_cancelled_continuations
        ):
            return None
        return self._append_tx(connection, draft).event

    def _start_attempt_tx(
        self,
        connection: sqlite3.Connection,
        draft: EventDraft,
        lease_seconds: float,
    ) -> Event | None:
        payload = draft.payload
        if (
            connection.execute(
                """
            SELECT 1 FROM attempts
            WHERE command_id = ? AND attempt_number = ?
            """,
                (payload.command_id, payload.attempt_number),
            ).fetchone()
            is not None
        ):
            return None
        command = connection.execute(
            "SELECT * FROM commands WHERE command_id = ?",
            (payload.command_id,),
        ).fetchone()
        if command is None or command["session_id"] != draft.session_id:
            raise KeyError(payload.command_id)
        if command["canonical_outcome_event_id"]:
            return None
        if (
            command["dispatch_eligible_event_id"] is None
            or draft.causation_id != command["dispatch_eligible_event_id"]
        ):
            return None
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM attempts WHERE command_id = ?",
            (payload.command_id,),
        ).fetchone()["count"]
        if payload.attempt_number != count + 1:
            return None
        return self._append_tx(
            connection,
            draft,
            attempt_lease_expires_at=self._clock() + lease_seconds,
        ).event

    def _authorization_causation_id_tx(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        command_id: str,
    ) -> str | None:
        command = connection.execute(
            "SELECT * FROM commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        if command is None or command["session_id"] != session_id:
            raise KeyError(command_id)
        if command["canonical_outcome_event_id"]:
            return None
        if command["dispatch_eligible_event_id"] is not None:
            return None
        if command["authorization_rejected_event_id"] is not None:
            return None
        if (
            connection.execute(
                "SELECT 1 FROM attempts WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            is not None
        ):
            return None
        return command["issued_event_id"]

    def _grant_command_tx(
        self,
        connection: sqlite3.Connection,
        draft: EventDraft,
    ) -> Event | None:
        payload = draft.payload
        causation_id = self._authorization_causation_id_tx(
            connection,
            draft.session_id,
            payload.command_id,
        )
        if causation_id is None:
            return None
        return self._append_tx(
            connection,
            replace(draft, causation_id=causation_id),
        ).event

    def _reject_command_tx(
        self,
        connection: sqlite3.Connection,
        draft: EventDraft,
    ) -> Event | None:
        payload = draft.payload
        causation_id = self._authorization_causation_id_tx(
            connection,
            draft.session_id,
            payload.command_id,
        )
        if causation_id is None:
            return None
        return self._append_tx(
            connection,
            replace(draft, causation_id=causation_id),
        ).event

    def _renew_attempt_tx(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        claim_token: str,
        lease_seconds: float,
    ) -> bool:
        now = self._clock()
        cursor = connection.execute(
            """
            UPDATE attempts SET claim_expires_at = ?
            WHERE attempt_id = ?
                AND claim_token = ?
                AND claim_expires_at != 0
                AND terminal_event_id IS NULL
            """,
            (now + lease_seconds, attempt_id, claim_token),
        )
        return cursor.rowcount == 1

    def _record_attempt_fact_tx(
        self,
        connection: sqlite3.Connection,
        draft: EventDraft,
    ) -> Event | None:
        payload = draft.payload
        existing = connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (draft.event_id,),
        ).fetchone()
        if existing is not None:
            event = self._event_from_row(existing)
            if not self._same_event(event, draft):
                raise ValueError(f"event id conflict: {draft.event_id}")
            return event

        attempt_id = payload.attempt_id
        existing = connection.execute(
            """
            SELECT events.* FROM attempts
            JOIN events ON events.event_id = attempts.terminal_event_id
            WHERE attempts.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if existing is not None:
            event = self._event_from_row(existing)
            if event.payload != payload:
                raise AttemptTerminalConflict(attempt_id)
            return event

        attempt = connection.execute(
            """
            SELECT attempts.command_id, attempts.dispatch_event_id
            FROM attempts
            WHERE attempts.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if attempt is None:
            raise KeyError(attempt_id)
        if attempt["command_id"] != payload.command_id:
            raise ValueError("attempt fact command mismatch")
        if draft.causation_id != attempt["dispatch_event_id"]:
            return None
        return self._append_tx(connection, draft).event

    def _index_event_tx(
        self,
        connection: sqlite3.Connection,
        event: Event,
        *,
        attempt_lease_expires_at: float | None = None,
    ) -> None:
        payload = event.payload
        if isinstance(payload, StepCommitted):
            step = payload.step
            connection.execute(
                """
                INSERT INTO decision_consumptions(
                    trigger_event_id, session_id, result_event_id, step_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    step.trigger_event_id,
                    event.session_id,
                    event.event_id,
                    step.step_id,
                ),
            )
            for command in step.commands:
                connection.execute(
                    """
                    INSERT INTO commands(
                        command_id,
                        session_id,
                        issued_event_id,
                        dispatch_eligible_event_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        command.command_id,
                        event.session_id,
                        event.event_id,
                        None if command.requires_authorization else event.event_id,
                    ),
                )
        elif isinstance(payload, CommandAuthorized):
            cursor = connection.execute(
                """
                UPDATE commands SET dispatch_eligible_event_id = ?
                WHERE command_id = ?
                    AND dispatch_eligible_event_id IS NULL
                    AND canonical_outcome_event_id IS NULL
                    AND authorization_rejected_event_id IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM attempts
                        WHERE attempts.command_id = commands.command_id
                    )
                """,
                (event.event_id, payload.command_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"command is not grantable: {payload.command_id}")
        elif isinstance(payload, CommandRejected):
            cursor = connection.execute(
                """
                UPDATE commands SET authorization_rejected_event_id = ?
                WHERE command_id = ?
                    AND authorization_rejected_event_id IS NULL
                    AND dispatch_eligible_event_id IS NULL
                    AND canonical_outcome_event_id IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM attempts WHERE command_id = ?
                    )
                """,
                (event.event_id, payload.command_id, payload.command_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"command is not rejectable: {payload.command_id}")
        elif isinstance(payload, DispatchAttemptStarted):
            if attempt_lease_expires_at is None:
                raise ValueError("attempt lease is required")
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id,
                    command_id,
                    attempt_number,
                    dispatch_event_id,
                    claim_token,
                    claim_expires_at,
                    worker_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.attempt_id,
                    payload.command_id,
                    payload.attempt_number,
                    event.event_id,
                    payload.claim_token,
                    attempt_lease_expires_at,
                    payload.worker_id,
                ),
            )
            connection.execute(
                """
                UPDATE commands SET dispatch_eligible_event_id = NULL
                WHERE command_id = ?
                """,
                (payload.command_id,),
            )
        elif isinstance(payload, CommandOutcomeReceived):
            current = connection.execute(
                """
                SELECT canonical_outcome_event_id FROM commands
                WHERE command_id = ?
                """,
                (payload.command_id,),
            ).fetchone()
            if current is None:
                raise KeyError(payload.command_id)
            if current["canonical_outcome_event_id"] is None:
                connection.execute(
                    """
                    UPDATE commands SET
                        canonical_outcome_event_id = ?,
                        dispatch_eligible_event_id = NULL
                    WHERE command_id = ?
                    """,
                    (event.event_id, payload.command_id),
                )
            if payload.attempt_id is not None:
                cursor = connection.execute(
                    """
                    UPDATE attempts SET terminal_event_id = ?
                    WHERE attempt_id = ? AND terminal_event_id IS NULL
                    """,
                    (event.event_id, payload.attempt_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"attempt already terminal: {payload.attempt_id}")
                self._clear_attempt_claims_tx(
                    connection,
                    payload.attempt_id,
                )
        elif isinstance(payload, DecisionCancelled):
            trigger = connection.execute(
                "SELECT session_id FROM events WHERE event_id = ?",
                (payload.trigger_event_id,),
            ).fetchone()
            if trigger is None or trigger["session_id"] != event.session_id:
                raise KeyError(payload.trigger_event_id)
            if event.causation_id != payload.trigger_event_id:
                raise ValueError("decision cancellation causation mismatch")
            if self._decision_consumed_tx(
                connection,
                payload.trigger_event_id,
            ):
                raise ValueError(
                    f"decision event consumed twice: {payload.trigger_event_id}"
                )
            connection.execute(
                """
                INSERT INTO decision_consumptions(
                    trigger_event_id, session_id, result_event_id, step_id
                ) VALUES (?, ?, ?, NULL)
                """,
                (payload.trigger_event_id, event.session_id, event.event_id),
            )
            connection.execute(
                "DELETE FROM step_claims WHERE session_id = ?",
                (event.session_id,),
            )
        elif isinstance(payload, StepContinuationCancelled):
            step = connection.execute(
                "SELECT session_id, event_type FROM events WHERE event_id = ?",
                (payload.step_event_id,),
            ).fetchone()
            if step is None or step["session_id"] != event.session_id:
                raise KeyError(payload.step_event_id)
            if step["event_type"] != "step.committed":
                raise ValueError(
                    f"continuation target is not a step: {payload.step_event_id}"
                )
            if event.causation_id != payload.step_event_id:
                raise ValueError("continuation cancellation causation mismatch")
            connection.execute(
                """
                INSERT INTO cancelled_step_continuations(
                    step_event_id, cancellation_event_id
                ) VALUES (?, ?)
                """,
                (payload.step_event_id, event.event_id),
            )

    def _decision_consumed_tx(
        self,
        connection: sqlite3.Connection,
        trigger_event_id: str,
    ) -> bool:
        return trigger_event_id in self._prefix_consumed or connection.execute(
            "SELECT 1 FROM decision_consumptions WHERE trigger_event_id = ?",
            (trigger_event_id,),
        ).fetchone() is not None

    @staticmethod
    def _clear_attempt_claims_tx(
        connection: sqlite3.Connection,
        attempt_id: str,
    ) -> None:
        connection.execute(
            """
            UPDATE attempts SET
                claim_expires_at = 0
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        )

    def _load_checkpoint_sync(
        self,
        session_id: str,
        journal_position: int,
        fingerprint: str,
    ) -> CanonicalState | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT state_json FROM checkpoints
                WHERE session_id = ?
                    AND journal_position = ?
                    AND fingerprint = ?
                    AND codec_version = ?
                    AND projection_version = ?
                """,
                (
                    session_id,
                    journal_position,
                    fingerprint,
                    STATE_CODEC_VERSION,
                    STATE_PROJECTION_VERSION,
                ),
            ).fetchone()
            return decode_state(row["state_json"]) if row is not None else None
        finally:
            connection.close()

    @staticmethod
    def _save_checkpoint_tx(
        connection: sqlite3.Connection,
        state: CanonicalState,
        fingerprint: str,
        state_json: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO sessions(session_id, last_sequence)
            VALUES (?, 0)
            """,
            (state.session_id,),
        )
        connection.execute(
            """
            INSERT INTO checkpoints(
                session_id,
                journal_position,
                fingerprint,
                codec_version,
                projection_version,
                state_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                journal_position = excluded.journal_position,
                fingerprint = excluded.fingerprint,
                codec_version = excluded.codec_version,
                projection_version = excluded.projection_version,
                state_json = excluded.state_json
            """,
            (
                state.session_id,
                state.journal_position,
                fingerprint,
                STATE_CODEC_VERSION,
                STATE_PROJECTION_VERSION,
                state_json,
            ),
        )

    @staticmethod
    def _validate_step_draft(
        request: StepClaimRequest,
        draft: EventDraft,
    ) -> None:
        payload = draft.payload
        step = payload.step
        if (
            draft.session_id != request.session_id
            or step.trigger_event_id != request.trigger_event_id
            or step.decision_cursor != request.decision_cursor
            or step.basis_state_version != request.basis_state_version
            or step.observed_journal_position != request.observed_journal_position
            or draft.causation_id != request.trigger_event_id
        ):
            raise ValueError("step does not match its claim")

    @staticmethod
    def _request_from_row(row: sqlite3.Row) -> StepClaimRequest:
        return StepClaimRequest(
            session_id=row["session_id"],
            trigger_event_id=row["trigger_event_id"],
            decision_cursor=row["decision_cursor"],
            basis_state_version=row["basis_state_version"],
            observed_journal_position=row["observed_journal_position"],
        )

    @classmethod
    def _lease_from_row(cls, row: sqlite3.Row) -> StepLease:
        return StepLease(
            request=cls._request_from_row(row),
            token=row["token"],
            owner_id=row["owner_id"],
            generation=row["generation"],
            expires_at=row["expires_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        delivery_source = row["delivery_source"]
        delivery_id = row["delivery_id"]
        if (delivery_source is None) != (delivery_id is None):
            raise ValueError("delivery identity columns must both be null or set")
        delivery = (
            DeliveryIdentity(delivery_source, delivery_id)
            if delivery_source is not None
            else None
        )
        artifact_refs = json.loads(row["artifact_refs_json"])
        if not isinstance(artifact_refs, list) or any(
            type(item) is not str or not item for item in artifact_refs
        ):
            raise ValueError("artifact_refs_json must be a string array")
        return Event(
            event_id=row["event_id"],
            session_id=row["session_id"],
            sequence=row["sequence"],
            payload=decode_payload(
                row["event_type"],
                row["schema_version"],
                row["payload_json"],
            ),
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            causation_id=row["causation_id"],
            correlation_id=row["correlation_id"],
            schema_version=row["schema_version"],
            artifact_refs=tuple(artifact_refs),
            delivery=delivery,
        )

    @staticmethod
    def _same_delivery(event: Event, draft: EventDraft) -> bool:
        return (
            event.session_id == draft.session_id
            and event.payload == draft.payload
            and event.causation_id == draft.causation_id
            and event.correlation_id == draft.correlation_id
            and event.schema_version == draft.schema_version
            and event.artifact_refs == draft.artifact_refs
        )

    @classmethod
    def _same_event(cls, event: Event, draft: EventDraft) -> bool:
        return cls._same_delivery(event, draft) and event.delivery == draft.delivery

    @staticmethod
    def _json_dump(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _validate_generic_append(draft: EventDraft) -> None:
        if draft.delivery is not None:
            raise ValueError("delivery events must use accept_delivery")
        if isinstance(draft.payload, DomainFactCommitted):
            raise ValueError("domain fact requires delivery identity")
        if isinstance(
            draft.payload,
            (
                StepCommitted,
                CommandAuthorized,
                CommandRejected,
                DecisionCancelled,
                StepContinuationCancelled,
                DispatchAttemptStarted,
                UserMessageReceived,
            ),
        ):
            raise ValueError("conditional event requires its Journal method")
        if isinstance(draft.payload, CommandOutcomeReceived):
            raise ValueError("attempt fact requires conditional append")

    @staticmethod
    def _validate_external_delivery(draft: EventDraft) -> None:
        if not isinstance(
            draft.payload,
            (UserMessageReceived, DomainFactCommitted),
        ):
            raise ValueError("delivery payload is not an external event")

    @staticmethod
    def _validate_internal_draft(draft: EventDraft) -> None:
        if draft.delivery is not None:
            raise ValueError("internal event cannot have delivery identity")
