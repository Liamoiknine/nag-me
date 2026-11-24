"""SQLite storage: one row of settings and a log of every call."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
COACH_STYLES = ("supportive", "strict", "sarcastic")

# Our own call lifecycle (Twilio's statuses are mapped onto these):
#   pending -> dialing -> answered -> completed
#                     \-> missed | failed
#   pending -> skipped  (a retry that fell outside work hours, or the schedule was paused)
OPEN_STATUSES = ("dialing", "answered")
FINAL_STATUSES = ("completed", "missed", "failed", "skipped")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    phone_number        TEXT    NOT NULL DEFAULT '',
    coach_style         TEXT    NOT NULL DEFAULT 'supportive',
    timezone            TEXT    NOT NULL DEFAULT 'America/Chicago',
    work_days           TEXT    NOT NULL DEFAULT 'mon,tue,wed,thu,fri',
    start_time          TEXT    NOT NULL DEFAULT '09:00',
    end_time            TEXT    NOT NULL DEFAULT '17:00',
    interval_minutes    INTEGER NOT NULL DEFAULT 60,
    max_retries         INTEGER NOT NULL DEFAULT 2,
    retry_delay_minutes INTEGER NOT NULL DEFAULT 5,
    active              INTEGER NOT NULL DEFAULT 0,
    next_call_at        TEXT
);
INSERT OR IGNORE INTO settings (id) VALUES (1);

CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT    NOT NULL,             -- scheduled | manual | retry
    attempt       INTEGER NOT NULL DEFAULT 0,   -- 0 for the first try, 1+ for retries
    scheduled_for TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    twilio_sid    TEXT,
    stream_token  TEXT,
    dialed_at     TEXT,
    answered_at   TEXT,
    ended_at      TEXT,
    transcript    TEXT,                         -- JSON list of {role, text}
    summary       TEXT,
    on_track      TEXT,                         -- yes | no | unclear
    plan          TEXT,
    error         TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_status_due ON calls (status, scheduled_for);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    # Fixed-width UTC timestamps so that string comparison in SQL matches time order.
    return value.astimezone(UTC).isoformat(timespec="microseconds") if value else None


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Settings:
    phone_number: str = ""
    coach_style: str = "supportive"
    timezone: str = "America/Chicago"
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)  # 0 = Monday
    start_time: time = time(9, 0)
    end_time: time = time(17, 0)
    interval_minutes: int = 60
    max_retries: int = 2
    retry_delay_minutes: int = 5
    active: bool = False
    next_call_at: datetime | None = None


@dataclass
class Call:
    id: int
    kind: str
    attempt: int
    scheduled_for: datetime
    status: str
    created_at: datetime
    twilio_sid: str | None = None
    stream_token: str | None = None
    dialed_at: datetime | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)
    summary: str | None = None
    on_track: str | None = None
    plan: str | None = None
    error: str | None = None


_CALL_TIME_FIELDS = ("scheduled_for", "created_at", "dialed_at", "answered_at", "ended_at")


def _row_to_call(row: sqlite3.Row) -> Call:
    data = dict(row)
    for name in _CALL_TIME_FIELDS:
        data[name] = from_iso(data[name])
    data["transcript"] = json.loads(data["transcript"]) if data["transcript"] else []
    return Call(**data)


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # Settings

    def get_settings(self) -> Settings:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return Settings(
            phone_number=row["phone_number"],
            coach_style=row["coach_style"],
            timezone=row["timezone"],
            work_days=tuple(DAY_NAMES.index(d) for d in row["work_days"].split(",") if d),
            start_time=time.fromisoformat(row["start_time"]),
            end_time=time.fromisoformat(row["end_time"]),
            interval_minutes=row["interval_minutes"],
            max_retries=row["max_retries"],
            retry_delay_minutes=row["retry_delay_minutes"],
            active=bool(row["active"]),
            next_call_at=from_iso(row["next_call_at"]),
        )

    def save_settings(self, s: Settings) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE settings SET phone_number = ?, coach_style = ?, timezone = ?,
                   work_days = ?, start_time = ?, end_time = ?, interval_minutes = ?,
                   max_retries = ?, retry_delay_minutes = ?, active = ?, next_call_at = ?
                   WHERE id = 1""",
                (
                    s.phone_number,
                    s.coach_style,
                    s.timezone,
                    ",".join(DAY_NAMES[d] for d in sorted(set(s.work_days))),
                    s.start_time.strftime("%H:%M"),
                    s.end_time.strftime("%H:%M"),
                    s.interval_minutes,
                    s.max_retries,
                    s.retry_delay_minutes,
                    int(s.active),
                    to_iso(s.next_call_at),
                ),
            )

    # Calls

    def create_call(self, kind: str, scheduled_for: datetime, attempt: int = 0) -> Call:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO calls (kind, attempt, scheduled_for, created_at) VALUES (?, ?, ?, ?)",
                (kind, attempt, to_iso(scheduled_for), to_iso(utcnow())),
            )
            call_id = cur.lastrowid
        call = self.get_call(call_id)
        assert call is not None
        return call

    def get_call(self, call_id: int) -> Call | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        return _row_to_call(row) if row else None

    def update_call(self, call_id: int, **values: Any) -> None:
        if not values:
            return
        for name, value in values.items():
            if isinstance(value, datetime):
                values[name] = to_iso(value)
            elif name == "transcript":
                values[name] = json.dumps(value)
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE calls SET {assignments} WHERE id = ?", (*values.values(), call_id)
            )

    def _select(self, where: str, params: tuple = (), tail: str = "") -> list[Call]:
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM calls WHERE {where} {tail}", params).fetchall()
        return [_row_to_call(r) for r in rows]

    def due_calls(self, now: datetime) -> list[Call]:
        return self._select(
            "status = 'pending' AND scheduled_for <= ?", (to_iso(now),), "ORDER BY scheduled_for"
        )

    def pending_calls(self) -> list[Call]:
        return self._select("status = 'pending'")

    def open_calls(self) -> list[Call]:
        return self._select("status IN ('dialing', 'answered')", (), "ORDER BY id")

    def calls_since(self, since: datetime) -> list[Call]:
        return self._select("scheduled_for >= ?", (to_iso(since),), "ORDER BY scheduled_for")

    def recent_calls(self, limit: int = 30) -> list[Call]:
        return self._select(
            "status != 'pending'", (), f"ORDER BY scheduled_for DESC LIMIT {int(limit)}"
        )
