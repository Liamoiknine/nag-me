"""SQLite storage: settings, calls, commitments, daily plans and Mac activity samples."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
COACH_STYLES = ("supportive", "strict", "sarcastic")
PURPOSES = ("morning", "deadline", "drift", "review", "manual", "inbound")

DEFAULT_DISTRACTIONS = """youtube.com
x.com
twitter.com
instagram.com
facebook.com
reddit.com
tiktok.com
netflix.com
twitch.tv
news.ycombinator.com"""

# Call lifecycle (Twilio's statuses are mapped onto these):
#   pending -> dialing -> answered -> completed
#                     \-> missed | failed
#   pending -> skipped  (paused, snoozed past the end of the day, or superseded)
OPEN_STATUSES = ("dialing", "answered")
FINAL_STATUSES = ("completed", "missed", "failed", "skipped")

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    phone_number        TEXT    NOT NULL DEFAULT '',
    coach_style         TEXT    NOT NULL DEFAULT 'supportive',
    timezone            TEXT    NOT NULL DEFAULT 'America/Chicago',
    work_days           TEXT    NOT NULL DEFAULT 'mon,tue,wed,thu,fri',
    start_time          TEXT    NOT NULL DEFAULT '09:00',
    end_time            TEXT    NOT NULL DEFAULT '17:00',
    morning_call        INTEGER NOT NULL DEFAULT 1,
    review_call         INTEGER NOT NULL DEFAULT 1,
    drift_enabled       INTEGER NOT NULL DEFAULT 1,
    drift_minutes       INTEGER NOT NULL DEFAULT 15,
    distractions        TEXT    NOT NULL DEFAULT '',
    max_retries         INTEGER NOT NULL DEFAULT 2,
    retry_delay_minutes INTEGER NOT NULL DEFAULT 5,
    active              INTEGER NOT NULL DEFAULT 0,
    snoozed_until       TEXT
);

CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose       TEXT    NOT NULL,             -- see PURPOSES
    attempt       INTEGER NOT NULL DEFAULT 0,   -- 0 for the first try, 1+ for retries
    scheduled_for TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    reason        TEXT,                         -- e.g. what the drift watcher saw
    twilio_sid    TEXT,
    stream_token  TEXT,
    dialed_at     TEXT,
    answered_at   TEXT,
    ended_at      TEXT,
    transcript    TEXT,                         -- JSON list of {role, text}
    summary       TEXT,
    on_track      TEXT,                         -- yes | no | unclear
    error         TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_status_due ON calls (status, scheduled_for);

CREATE TABLE IF NOT EXISTS commitments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT    NOT NULL,
    due_at      TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'open',  -- open | done | missed | dropped
    checked_at  TEXT,                             -- when a deadline call was queued for it
    resolved_at TEXT,
    source      TEXT    NOT NULL DEFAULT 'call',  -- call | text | menubar | dashboard
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS commitments_due ON commitments (status, due_at);

CREATE TABLE IF NOT EXISTS days (
    day  TEXT PRIMARY KEY,                        -- local date, YYYY-MM-DD
    plan TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    at         TEXT    NOT NULL,
    app        TEXT,
    domain     TEXT,
    distracted INTEGER NOT NULL,
    idle       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_at ON activity (at);
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
    morning_call: bool = True
    review_call: bool = True
    drift_enabled: bool = True
    drift_minutes: int = 15
    distractions: str = DEFAULT_DISTRACTIONS
    max_retries: int = 2
    retry_delay_minutes: int = 5
    active: bool = False
    snoozed_until: datetime | None = None

    @property
    def distraction_list(self) -> list[str]:
        return [line.strip().lower() for line in self.distractions.splitlines() if line.strip()]


@dataclass
class Call:
    id: int
    purpose: str
    attempt: int
    scheduled_for: datetime
    status: str
    created_at: datetime
    reason: str | None = None
    twilio_sid: str | None = None
    stream_token: str | None = None
    dialed_at: datetime | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)
    summary: str | None = None
    on_track: str | None = None
    error: str | None = None


@dataclass
class Commitment:
    id: int
    text: str
    due_at: datetime
    status: str
    source: str
    created_at: datetime
    checked_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass
class Sample:
    at: datetime
    app: str | None
    domain: str | None
    distracted: bool
    idle: bool


_CALL_TIMES = ("scheduled_for", "created_at", "dialed_at", "answered_at", "ended_at")
_COMMITMENT_TIMES = ("due_at", "created_at", "checked_at", "resolved_at")


def _row_to_call(row: sqlite3.Row) -> Call:
    data = dict(row)
    for name in _CALL_TIMES:
        data[name] = from_iso(data[name])
    data["transcript"] = json.loads(data["transcript"]) if data["transcript"] else []
    return Call(**data)


def _row_to_commitment(row: sqlite3.Row) -> Commitment:
    data = dict(row)
    for name in _COMMITMENT_TIMES:
        data[name] = from_iso(data[name])
    return Commitment(**data)


def _serialize(values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, value in values.items():
        if isinstance(value, datetime):
            value = to_iso(value)
        elif name == "transcript":
            value = json.dumps(value)
        elif isinstance(value, bool):
            value = int(value)
        out[name] = value
    return out


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if 0 < version < SCHEMA_VERSION or (version == 0 and _has_v1_tables(conn)):
                # v1 was a prototype with a different schema; start fresh.
                conn.executescript("DROP TABLE IF EXISTS settings; DROP TABLE IF EXISTS calls;")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO settings (id, distractions) VALUES (1, ?)",
                (DEFAULT_DISTRACTIONS,),
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
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
            morning_call=bool(row["morning_call"]),
            review_call=bool(row["review_call"]),
            drift_enabled=bool(row["drift_enabled"]),
            drift_minutes=row["drift_minutes"],
            distractions=row["distractions"],
            max_retries=row["max_retries"],
            retry_delay_minutes=row["retry_delay_minutes"],
            active=bool(row["active"]),
            snoozed_until=from_iso(row["snoozed_until"]),
        )

    def save_settings(self, s: Settings) -> None:
        values = _serialize(
            {
                "phone_number": s.phone_number,
                "coach_style": s.coach_style,
                "timezone": s.timezone,
                "work_days": ",".join(DAY_NAMES[d] for d in sorted(set(s.work_days))),
                "start_time": s.start_time.strftime("%H:%M"),
                "end_time": s.end_time.strftime("%H:%M"),
                "morning_call": s.morning_call,
                "review_call": s.review_call,
                "drift_enabled": s.drift_enabled,
                "drift_minutes": s.drift_minutes,
                "distractions": s.distractions,
                "max_retries": s.max_retries,
                "retry_delay_minutes": s.retry_delay_minutes,
                "active": s.active,
                "snoozed_until": s.snoozed_until,
            }
        )
        assignments = ", ".join(f"{k} = ?" for k in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE settings SET {assignments} WHERE id = 1", tuple(values.values()))

    # Calls

    def create_call(
        self, purpose: str, scheduled_for: datetime, attempt: int = 0, reason: str | None = None
    ) -> Call:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO calls (purpose, attempt, scheduled_for, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (purpose, attempt, to_iso(scheduled_for), reason, to_iso(utcnow())),
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
        values = _serialize(values)
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE calls SET {assignments} WHERE id = ?", (*values.values(), call_id)
            )

    def _calls(self, where: str, params: tuple = (), tail: str = "") -> list[Call]:
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM calls WHERE {where} {tail}", params).fetchall()
        return [_row_to_call(r) for r in rows]

    def due_calls(self, now: datetime) -> list[Call]:
        return self._calls(
            "status = 'pending' AND scheduled_for <= ?", (to_iso(now),), "ORDER BY scheduled_for"
        )

    def pending_calls(self) -> list[Call]:
        return self._calls("status = 'pending'", (), "ORDER BY scheduled_for")

    def open_calls(self) -> list[Call]:
        return self._calls("status IN ('dialing', 'answered')", (), "ORDER BY id")

    def calls_between(self, start: datetime, end: datetime) -> list[Call]:
        return self._calls(
            "scheduled_for >= ? AND scheduled_for < ?",
            (to_iso(start), to_iso(end)),
            "ORDER BY scheduled_for",
        )

    def recent_calls(self, limit: int = 30) -> list[Call]:
        return self._calls(
            "status != 'pending'", (), f"ORDER BY scheduled_for DESC LIMIT {int(limit)}"
        )

    # Commitments

    def add_commitment(self, text: str, due_at: datetime, source: str) -> Commitment:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO commitments (text, due_at, source, created_at) VALUES (?, ?, ?, ?)",
                (text, to_iso(due_at), source, to_iso(utcnow())),
            )
            commitment_id = cur.lastrowid
        found = self.get_commitment(commitment_id)
        assert found is not None
        return found

    def get_commitment(self, commitment_id: int) -> Commitment | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
            ).fetchone()
        return _row_to_commitment(row) if row else None

    def update_commitment(self, commitment_id: int, **values: Any) -> None:
        values = _serialize(values)
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE commitments SET {assignments} WHERE id = ?",
                (*values.values(), commitment_id),
            )

    def _commitments(self, where: str, params: tuple = ()) -> list[Commitment]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM commitments WHERE {where} ORDER BY due_at", params
            ).fetchall()
        return [_row_to_commitment(r) for r in rows]

    def open_commitments(self) -> list[Commitment]:
        return self._commitments("status = 'open'")

    def unchecked_due_commitments(self, now: datetime) -> list[Commitment]:
        return self._commitments(
            "status = 'open' AND checked_at IS NULL AND due_at <= ?", (to_iso(now),)
        )

    def commitments_between(self, start: datetime, end: datetime) -> list[Commitment]:
        return self._commitments("due_at >= ? AND due_at < ?", (to_iso(start), to_iso(end)))

    # Daily plans

    def get_plan(self, day: date) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT plan FROM days WHERE day = ?", (day.isoformat(),)).fetchone()
        return row["plan"] if row else None

    def set_plan(self, day: date, plan: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO days (day, plan) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET plan = excluded.plan",
                (day.isoformat(), plan),
            )

    # Activity

    def add_sample(self, sample: Sample) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO activity (at, app, domain, distracted, idle) VALUES (?, ?, ?, ?, ?)",
                (
                    to_iso(sample.at),
                    sample.app,
                    sample.domain,
                    int(sample.distracted),
                    int(sample.idle),
                ),
            )

    def samples_between(self, start: datetime, end: datetime) -> list[Sample]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM activity WHERE at >= ? AND at < ? ORDER BY at",
                (to_iso(start), to_iso(end)),
            ).fetchall()
        return [
            Sample(from_iso(r["at"]), r["app"], r["domain"], bool(r["distracted"]), bool(r["idle"]))
            for r in rows
        ]

    def prune_activity(self, before: datetime) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM activity WHERE at < ?", (to_iso(before),))


def _has_v1_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'calls'"
    ).fetchone()
    return bool(row and "kind" in row[0])
