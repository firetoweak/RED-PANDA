from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable


MAX_DELAY_SECONDS = 365 * 24 * 60 * 60
TIME_REACHED = "automation.time_reached"
SOURCE = "automation"


class ScheduleDeliveryUnavailable(Exception):
    """The Session Worker could not accept a due fact yet."""


@dataclass(frozen=True, slots=True)
class ScheduledCheck:
    schedule_id: str
    session_id: str
    delay_seconds: int
    purpose: str
    due_at: datetime


class OneShotSchedules:
    """Durable clock entries. The Session Journal owns the delivered fact."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"unsupported automation schema version: {version}")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    schedule_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    delay_seconds INTEGER NOT NULL,
                    purpose TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    fired_at TEXT,
                    delivered_at TEXT
                )
                """
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schedule_cancellations (
                    command_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    schedule_id TEXT NOT NULL,
                    code TEXT NOT NULL CHECK (code IN ('CANCELLED', 'ALREADY_FIRED', 'NOT_FOUND'))
                )"""
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

    def register(
        self,
        schedule_id: str,
        session_id: str,
        delay_seconds: int,
        purpose: str,
        *,
        started_at: datetime | None = None,
    ) -> ScheduledCheck:
        if type(schedule_id) is not str or not schedule_id:
            raise ValueError("schedule_id must be a non-empty string")
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if type(delay_seconds) is not int or not 1 <= delay_seconds <= MAX_DELAY_SECONDS:
            raise ValueError("delay_seconds must be between 1 and 31536000")
        if type(purpose) is not str or not purpose.strip():
            raise ValueError("purpose must be a non-empty string")
        if started_at is not None and started_at.tzinfo is not timezone.utc:
            raise ValueError("schedule started_at must use UTC")
        due_at = (started_at or datetime.now(timezone.utc)) + timedelta(
            seconds=delay_seconds
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                connection.execute(
                    """INSERT OR IGNORE INTO schedules
                    (schedule_id, session_id, delay_seconds, purpose, due_at, fired_at, delivered_at)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL)""",
                    (schedule_id, session_id, delay_seconds, purpose, due_at.isoformat()),
                )
                row = connection.execute(
                    "SELECT * FROM schedules WHERE schedule_id = ?", (schedule_id,)
                ).fetchone()
        schedule = self._from_row(row)
        if (
            schedule.session_id != session_id
            or schedule.delay_seconds != delay_seconds
            or schedule.purpose != purpose
        ):
            raise ValueError("schedule_id already belongs to another request")
        return schedule

    def next_pending(self, session_id: str | None = None) -> ScheduledCheck | None:
        sql = """SELECT * FROM schedules WHERE delivered_at IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM schedule_cancellations AS cancellation
                WHERE cancellation.schedule_id = schedules.schedule_id
                AND cancellation.code = 'CANCELLED'
            )"""
        values: tuple[str, ...] = ()
        if session_id is not None:
            sql += " AND session_id = ?"
            values = (session_id,)
        sql += " ORDER BY due_at, schedule_id LIMIT 1"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(sql, values).fetchone()
        return None if row is None else self._from_row(row)

    def cancel(self, command_id: str, session_id: str, schedule_id: str) -> str:
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT session_id, schedule_id, code FROM schedule_cancellations "
                    "WHERE command_id = ?", (command_id,),
                ).fetchone()
                if previous is not None:
                    if previous[:2] != (session_id, schedule_id):
                        raise ValueError("cancellation command_id belongs to another request")
                    return previous[2]
                row = connection.execute(
                    "SELECT fired_at FROM schedules WHERE schedule_id = ? AND session_id = ?",
                    (schedule_id, session_id),
                ).fetchone()
                if row is None:
                    code = "NOT_FOUND"
                elif row[0] is not None:
                    code = "ALREADY_FIRED"
                else:
                    code = "CANCELLED"
                connection.execute(
                    "INSERT INTO schedule_cancellations VALUES (?, ?, ?, ?)",
                    (command_id, session_id, schedule_id, code),
                )
        return code

    def delivered(self, schedule_id: str) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE schedules SET delivered_at = ? "
                    "WHERE schedule_id = ? AND delivered_at IS NULL",
                    (datetime.now(timezone.utc).isoformat(), schedule_id),
                )

    def firing_time(self, schedule_id: str) -> datetime | None:
        now = datetime.now(timezone.utc)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE schedules SET fired_at = ? "
                    "WHERE schedule_id = ? AND fired_at IS NULL "
                    "AND NOT EXISTS (SELECT 1 FROM schedule_cancellations AS cancellation "
                    "WHERE cancellation.schedule_id = schedules.schedule_id "
                    "AND cancellation.code = 'CANCELLED')",
                    (now.isoformat(), schedule_id),
                )
                row = connection.execute(
                    "SELECT fired_at FROM schedules WHERE schedule_id = ?",
                    (schedule_id,),
                ).fetchone()
        if row[0] is None:
            return None
        fired_at = datetime.fromisoformat(row[0])
        if fired_at.tzinfo is not timezone.utc:
            raise ValueError("schedule fired_at must use UTC")
        return fired_at

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ScheduledCheck:
        due_at = datetime.fromisoformat(row["due_at"])
        if due_at.tzinfo is not timezone.utc:
            raise ValueError("schedule due_at must use UTC")
        return ScheduledCheck(
            row["schedule_id"],
            row["session_id"],
            row["delay_seconds"],
            row["purpose"],
            due_at,
        )


class OneShotClock:
    def __init__(self, schedules: OneShotSchedules) -> None:
        self.schedules = schedules
        self.changed = asyncio.Event()

    def register(
        self,
        schedule_id: str,
        session_id: str,
        delay_seconds: int,
        purpose: str,
        *,
        started_at: datetime | None = None,
    ) -> ScheduledCheck:
        schedule = self.schedules.register(
            schedule_id,
            session_id,
            delay_seconds,
            purpose,
            started_at=started_at,
        )
        self.changed.set()
        return schedule

    def cancel(self, command_id: str, session_id: str, schedule_id: str) -> str:
        code = self.schedules.cancel(command_id, session_id, schedule_id)
        if code == "CANCELLED":
            self.changed.set()
        return code

    async def run(
        self,
        deliver: Callable[[ScheduledCheck, datetime], Awaitable[None]],
        changed: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        while True:
            schedule = self.schedules.next_pending()
            self.changed.clear()
            if schedule is None:
                await self.changed.wait()
                continue
            seconds = (schedule.due_at - datetime.now(timezone.utc)).total_seconds()
            if seconds > 0:
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=seconds)
                except TimeoutError:
                    pass
                else:
                    continue
            fired_at = self.schedules.firing_time(schedule.schedule_id)
            if fired_at is None:
                continue
            try:
                await deliver(schedule, fired_at)
            except ScheduleDeliveryUnavailable:
                await asyncio.sleep(30)
                continue
            self.schedules.delivered(schedule.schedule_id)
            if changed is not None:
                await changed(schedule.session_id)
