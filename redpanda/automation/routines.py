"""Routines: an Agent's standing orders, woken on a schedule by the Host clock.

A routine stores a name, the intent its owner wrote for its future self, and a
schedule. The Host decides only *when*; what to do each time is the owner's call
when the reminder arrives. Every operation is keyed by the Command that asked for
it, so recovery may replay a request without applying it twice.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from croniter import croniter

from redpanda.automation.once import ScheduleDeliveryUnavailable


ROUTINE_DUE = "automation.routine_due"
MIN_INTERVAL = timedelta(minutes=5)
MAX_NAME_CHARS = 80
MAX_INTENT_CHARS = 4000
_EVERY = re.compile(r"^@every\s+(\d+)([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
ACTIVE, PAUSED, PAUSED_BY_USER, PAUSED_UNWATCHED, DELETED = (
    "active", "paused", "paused_by_user", "paused_unwatched", "deleted",
)


class RoutineError(ValueError):
    """A request the owner can correct; the code and message are shown to it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RoutineNotFound(LookupError):
    """The person asked about a routine this Agent does not have (or no longer has)."""


@dataclass(frozen=True, slots=True)
class Routine:
    routine_id: str
    session_id: str
    name: str
    intent: str
    schedule: str
    state: str
    revision: int
    next_due: datetime | None
    last_fired_at: datetime | None

    def for_model(self) -> dict[str, object]:
        return {
            "routine_id": self.routine_id,
            "name": self.name,
            "intent": self.intent,
            "schedule": self.schedule,
            "state": self.state,
            "next_run": None if self.next_due is None else _local(self.next_due),
            "last_run": None if self.last_fired_at is None else _local(self.last_fired_at),
        }


@dataclass(frozen=True, slots=True)
class RoutineFire:
    fire_id: str
    routine_id: str
    session_id: str
    name: str
    intent: str
    planned_at: datetime
    fired_at: datetime
    delivered_at: datetime | None = None


def next_run(schedule: str, after: datetime) -> datetime:
    """The first run strictly after ``after``, in UTC; cron reads the Host's local time."""
    every = _EVERY.match(schedule)
    if every is not None:
        return after + timedelta(**{_UNITS[every.group(2)]: int(every.group(1))})
    local = croniter(schedule, after.astimezone()).get_next(datetime)
    return local.astimezone(timezone.utc)


def validate_schedule(schedule: str) -> None:
    every = _EVERY.match(schedule)
    if every is not None:
        if timedelta(**{_UNITS[every.group(2)]: int(every.group(1))}) < MIN_INTERVAL:
            raise RoutineError("SCHEDULE_TOO_FREQUENT", "两次运行至少间隔 5 分钟。")
        return
    if not croniter.is_valid(schedule) or len(schedule.split()) != 5:
        raise RoutineError(
            "INVALID_SCHEDULE",
            "schedule 需要是 5 段 cron（如 '*/10 9-18 * * 1-5'）或 '@every 30m' / '@every 2h' / '@every 1d'。",
        )
    runs = croniter(schedule, datetime.now().astimezone())
    times = [runs.get_next(datetime) for _ in range(10)]
    if any(later - earlier < MIN_INTERVAL for earlier, later in zip(times, times[1:])):
        raise RoutineError("SCHEDULE_TOO_FREQUENT", "两次运行至少间隔 5 分钟。")


class RoutineStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 3):
                raise ValueError(f"unsupported routine schema version: {version}")
            states = f"'{ACTIVE}', '{PAUSED}', '{PAUSED_BY_USER}', '{PAUSED_UNWATCHED}', '{DELETED}'"
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS routines (
                    routine_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ({states})),
                    revision INTEGER NOT NULL,
                    next_due TEXT,
                    last_fired_at TEXT
                );
                CREATE TABLE IF NOT EXISTS routine_requests (
                    command_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS routine_fires (
                    fire_id TEXT PRIMARY KEY,
                    routine_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    planned_at TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                """
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS absence_watch (
                    session_id TEXT PRIMARY KEY,
                    phase TEXT NOT NULL CHECK (phase IN ('asked', 'paused')),
                    asked_at TEXT NOT NULL,
                    paused_ids TEXT NOT NULL,
                    fact_recorded INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_presence (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_seen TEXT,
                    connections INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO web_presence VALUES (1, NULL, 0);
                UPDATE web_presence SET connections = 0 WHERE id = 1;
                PRAGMA user_version = 3;
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _once(self, command_id: str, session_id: str, apply) -> dict[str, object]:
        """Apply a request at most once; a replay returns the first result."""
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT session_id, result FROM routine_requests WHERE command_id = ?", (command_id,),
                ).fetchone()
                if previous is not None:
                    if previous["session_id"] != session_id:
                        raise ValueError("routine command_id belongs to another session")
                    return json.loads(previous["result"])
                try:
                    result = apply(connection)
                except RoutineError as error:
                    result = {"ok": False, "code": error.code, "error": str(error)}
                connection.execute(
                    "INSERT INTO routine_requests VALUES (?, ?, ?)",
                    (command_id, session_id, json.dumps(result, ensure_ascii=False)),
                )
                return result

    def save(self, command_id: str, session_id: str, *, routine_id: str | None,
             name: str, intent: str, schedule: str) -> dict[str, object]:
        def apply(connection):
            if not name.strip() or len(name) > MAX_NAME_CHARS:
                raise RoutineError("INVALID_NAME", f"name 需要 1 到 {MAX_NAME_CHARS} 个字符。")
            if not intent.strip() or len(intent) > MAX_INTENT_CHARS:
                raise RoutineError("INVALID_INTENT", f"intent 需要 1 到 {MAX_INTENT_CHARS} 个字符。")
            validate_schedule(schedule)
            now = datetime.now(timezone.utc)
            if routine_id is None:
                connection.execute(
                    "INSERT INTO routines VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL)",
                    (command_id, session_id, name, intent, schedule, ACTIVE,
                     next_run(schedule, now).isoformat()),
                )
                saved = command_id
            else:
                row = self._owned(connection, session_id, routine_id)
                next_due = next_run(schedule, now).isoformat() if row["state"] == ACTIVE else None
                connection.execute(
                    "UPDATE routines SET name = ?, intent = ?, schedule = ?, revision = revision + 1, "
                    "next_due = ? WHERE routine_id = ?",
                    (name, intent, schedule, next_due, routine_id),
                )
                saved = routine_id
            return {"ok": True, "code": "ROUTINE_SAVED",
                    "data": self._routine(connection, saved).for_model()}
        return self._once(command_id, session_id, apply)

    def set_state(self, command_id: str, session_id: str, routine_id: str, state: str) -> dict[str, object]:
        def apply(connection):
            row = self._owned(connection, session_id, routine_id)
            if row["state"] == PAUSED_BY_USER:
                raise RoutineError("PAUSED_BY_USER", "这个例行任务被人暂停了，只能由人恢复或删除。")
            if row["state"] == PAUSED_UNWATCHED:
                raise RoutineError(
                    "PAUSED_UNWATCHED", "这个例行任务因无人查看被暂停，只能由人恢复或删除。",
                )
            self._write_state(connection, row, state)
            if state == DELETED:
                return {"ok": True, "code": "ROUTINE_DELETED", "data": {"routine_id": routine_id}}
            return {"ok": True, "code": "ROUTINE_UPDATED",
                    "data": self._routine(connection, routine_id).for_model()}
        return self._once(command_id, session_id, apply)

    def set_state_by_user(self, routine_id: str, state: str) -> Routine | None:
        """A person pausing, resuming or deleting from the app; the Agent cannot undo a pause."""
        with closing(self._connect()) as connection:
            with connection:
                row = connection.execute(
                    "SELECT * FROM routines WHERE routine_id = ? AND state != ?", (routine_id, DELETED),
                ).fetchone()
                if row is None:
                    raise RoutineNotFound(routine_id)
                self._write_state(connection, row, state)
                return None if state == DELETED else self._routine(connection, routine_id)

    @staticmethod
    def _write_state(connection, row, state) -> None:
        next_due = (next_run(row["schedule"], datetime.now(timezone.utc)).isoformat()
                    if state == ACTIVE else None)
        connection.execute(
            "UPDATE routines SET state = ?, next_due = ? WHERE routine_id = ?",
            (state, next_due, row["routine_id"]),
        )

    def _owned(self, connection, session_id: str, routine_id: str):
        row = connection.execute(
            "SELECT * FROM routines WHERE routine_id = ? AND session_id = ? AND state != ?",
            (routine_id, session_id, DELETED),
        ).fetchone()
        if row is None:
            raise RoutineError("ROUTINE_NOT_FOUND", "没有这个例行任务；用 list_routines 查看现有的。")
        return row

    def _routine(self, connection, routine_id: str) -> Routine:
        return _from_row(connection.execute(
            "SELECT * FROM routines WHERE routine_id = ?", (routine_id,)).fetchone())

    def delete_session(self, session_id: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("UPDATE routines SET state = ?, next_due = NULL WHERE session_id = ?",
                                   (DELETED, session_id))
                connection.execute("DELETE FROM absence_watch WHERE session_id = ?", (session_id,))

    def list(self, session_id: str | None = None) -> tuple[Routine, ...]:
        sql, values = "SELECT * FROM routines WHERE state != ?", [DELETED]
        if session_id is not None:
            sql += " AND session_id = ?"
            values.append(session_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql + " ORDER BY name, routine_id", values).fetchall()
        return tuple(_from_row(row) for row in rows)

    def fires(self, session_id: str, routine_id: str) -> tuple[RoutineFire, ...]:
        """This Agent's recorded runs, newest first. A deleted routine is gone."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state FROM routines WHERE routine_id = ? AND session_id = ?",
                (routine_id, session_id),
            ).fetchone()
            if row is None or row["state"] == DELETED:
                raise RoutineNotFound(routine_id)
            rows = connection.execute(
                "SELECT * FROM routine_fires WHERE routine_id = ? "
                "ORDER BY fired_at DESC, fire_id DESC",
                (routine_id,),
            ).fetchall()
        return tuple(_fire_from_row(row) for row in rows)

    def undelivered(self) -> RoutineFire | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM routine_fires WHERE delivered_at IS NULL "
                "ORDER BY fired_at, fire_id LIMIT 1",
            ).fetchone()
        return None if row is None else _fire_from_row(row)

    def next_due(self) -> Routine | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM routines WHERE state = ? AND next_due IS NOT NULL "
                "ORDER BY next_due, routine_id LIMIT 1", (ACTIVE,),
            ).fetchone()
        return None if row is None else _from_row(row)

    def fire(self, routine: Routine) -> RoutineFire | None:
        """Record the fire and advance the schedule together; a changed routine is not fired."""
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM routines WHERE routine_id = ?", (routine.routine_id,)).fetchone()
                current = _from_row(row)
                if (current.state != ACTIVE or current.revision != routine.revision
                        or current.next_due != routine.next_due):
                    return None
                fire_id = f"{routine.routine_id}:{routine.revision}:{routine.next_due.isoformat()}"
                connection.execute(
                    "INSERT INTO routine_fires VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                    (fire_id, current.routine_id, current.session_id, current.name, current.intent,
                     current.next_due.isoformat(), now.isoformat()),
                )
                # The clock may wake a moment early; never schedule the slot just fired again.
                after = max(now, routine.next_due)
                next_due = next_run(routine.schedule, after)
                if _EVERY.match(routine.schedule) is not None:
                    interval = next_run(routine.schedule, routine.next_due) - routine.next_due
                    next_due = routine.next_due + ((after - routine.next_due) // interval + 1) * interval
                connection.execute(
                    "UPDATE routines SET next_due = ?, last_fired_at = ? WHERE routine_id = ?",
                    (next_due.isoformat(), now.isoformat(), routine.routine_id),
                )
        return RoutineFire(fire_id, routine.routine_id, routine.session_id, routine.name, routine.intent,
                           routine.next_due, now)

    def delivered(self, fire_id: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE routine_fires SET delivered_at = ? WHERE fire_id = ? AND delivered_at IS NULL",
                    (datetime.now(timezone.utc).isoformat(), fire_id),
                )

    def presence(self) -> tuple[datetime | None, int]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT last_seen, connections FROM web_presence WHERE id = 1").fetchone()
        return _utc(row["last_seen"]), row["connections"]

    def note_connected(self, now: datetime) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE web_presence SET connections = connections + 1, last_seen = ? WHERE id = 1",
                    (now.isoformat(),),
                )

    def note_disconnected(self, now: datetime) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE web_presence SET
                        connections = MAX(connections - 1, 0),
                        last_seen = CASE WHEN connections - 1 <= 0 THEN ? ELSE last_seen END
                    WHERE id = 1
                    """,
                    (now.isoformat(),),
                )

    def watches(self) -> tuple[AbsenceWatch, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM absence_watch ORDER BY session_id",
            ).fetchall()
        return tuple(_watch_from_row(row) for row in rows)

    def watch(self, session_id: str) -> AbsenceWatch | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM absence_watch WHERE session_id = ?", (session_id,),
            ).fetchone()
        return None if row is None else _watch_from_row(row)

    def ask_absence(self, session_id: str, now: datetime) -> AbsenceWatch:
        """Open the inbox question once; a repeat keeps the original time."""
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO absence_watch VALUES (?, 'asked', ?, '[]', 0)",
                    (session_id, now.isoformat()),
                )
                row = connection.execute(
                    "SELECT * FROM absence_watch WHERE session_id = ?", (session_id,),
                ).fetchone()
        return _watch_from_row(row)

    def pause_for_absence(self, session_id: str) -> AbsenceWatch:
        """Pause routines that are still running. A second call does not pause new ones."""
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT * FROM absence_watch WHERE session_id = ?", (session_id,),
                ).fetchone()
                if current is not None and current["phase"] == "paused":
                    return _watch_from_row(current)
                active = connection.execute(
                    "SELECT routine_id FROM routines WHERE session_id = ? AND state = ? ORDER BY routine_id",
                    (session_id, ACTIVE),
                ).fetchall()
                for row in active:
                    connection.execute(
                        "UPDATE routines SET state = ?, next_due = NULL WHERE routine_id = ?",
                        (PAUSED_UNWATCHED, row["routine_id"]),
                    )
                ids = [row["routine_id"] for row in active]
                asked_at = current["asked_at"] if current is not None else datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    INSERT INTO absence_watch (session_id, phase, asked_at, paused_ids, fact_recorded)
                    VALUES (?, 'paused', ?, ?, 0)
                    ON CONFLICT(session_id) DO UPDATE SET
                        phase = 'paused', paused_ids = excluded.paused_ids, fact_recorded = 0
                    """,
                    (session_id, asked_at, json.dumps(ids)),
                )
                row = connection.execute(
                    "SELECT * FROM absence_watch WHERE session_id = ?", (session_id,),
                ).fetchone()
        return _watch_from_row(row)

    def mark_absence_told(self, session_id: str) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "UPDATE absence_watch SET fact_recorded = 1 WHERE session_id = ? AND phase = 'paused'",
                    (session_id,),
                )

    def resume_absence(self, session_id: str, now: datetime) -> tuple[str, ...]:
        """Restore only routines this pause still holds, then clear the question."""
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT * FROM absence_watch WHERE session_id = ?", (session_id,),
                ).fetchone()
                if current is None:
                    return ()
                restored: list[str] = []
                for routine_id in json.loads(current["paused_ids"]):
                    row = connection.execute(
                        "SELECT * FROM routines WHERE routine_id = ? AND state = ?",
                        (routine_id, PAUSED_UNWATCHED),
                    ).fetchone()
                    if row is None:
                        continue
                    connection.execute(
                        "UPDATE routines SET state = ?, next_due = ? WHERE routine_id = ?",
                        (ACTIVE, next_run(row["schedule"], now).isoformat(), routine_id),
                    )
                    restored.append(routine_id)
                connection.execute("DELETE FROM absence_watch WHERE session_id = ?", (session_id,))
        return tuple(restored)


@dataclass(frozen=True, slots=True)
class AbsenceWatch:
    session_id: str
    phase: str
    asked_at: datetime
    paused_ids: tuple[str, ...]
    fact_recorded: bool


class RoutineClock:
    def __init__(self, store: RoutineStore) -> None:
        self.store = store
        self.changed = asyncio.Event()

    async def run(
        self,
        deliver: Callable[[RoutineFire], Awaitable[None]],
        changed: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        while True:
            self.changed.clear()
            fire = self.store.undelivered()
            if fire is None:
                routine = self.store.next_due()
                if routine is None:
                    await self.changed.wait()
                    continue
                seconds = (routine.next_due - datetime.now(timezone.utc)).total_seconds()
                if seconds > 0:
                    try:
                        await asyncio.wait_for(self.changed.wait(), timeout=seconds)
                    except TimeoutError:
                        pass
                    continue
                fire = self.store.fire(routine)
                if fire is None:
                    continue
            try:
                await deliver(fire)
            except ScheduleDeliveryUnavailable:
                await asyncio.sleep(30)
                continue
            self.store.delivered(fire.fire_id)
            if changed is not None:
                await changed(fire.session_id)


def _utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not timezone.utc:
        raise ValueError("routine times must use UTC")
    return parsed


def _local(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="minutes")


def _from_row(row) -> Routine:
    return Routine(row["routine_id"], row["session_id"], row["name"], row["intent"], row["schedule"],
                   row["state"], row["revision"], _utc(row["next_due"]), _utc(row["last_fired_at"]))


def _watch_from_row(row) -> AbsenceWatch:
    return AbsenceWatch(
        row["session_id"], row["phase"], _utc(row["asked_at"]),
        tuple(json.loads(row["paused_ids"])), bool(row["fact_recorded"]),
    )


def _fire_from_row(row) -> RoutineFire:
    return RoutineFire(
        row["fire_id"], row["routine_id"], row["session_id"], row["name"], row["intent"],
        _utc(row["planned_at"]), _utc(row["fired_at"]), _utc(row["delivered_at"]),
    )
