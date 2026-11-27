"""Daily and weekly numbers: what you planned versus did, drift and dodged calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .calls import CallManager
from .schedule import day_bounds, local_date


@dataclass
class DayScore:
    day: date
    plan: str | None
    committed: int
    done: int
    missed: int  # marked not done, or still open after the deadline
    open: int
    drift_minutes: int
    answered: int
    dodged: int

    @property
    def hit_rate(self) -> int | None:
        settled = self.done + self.missed
        return round(100 * self.done / settled) if settled else None


def score_day(manager: CallManager, day: date, now: datetime) -> DayScore:
    s = manager.db.get_settings()
    start, end = day_bounds(day, s)
    commitments = [c for c in manager.db.commitments_between(start, end) if c.status != "dropped"]
    calls = manager.db.calls_between(start, end)
    overdue = [c for c in commitments if c.status == "open" and c.due_at < now]
    return DayScore(
        day=day,
        plan=manager.db.get_plan(day),
        committed=len(commitments),
        done=sum(c.status == "done" for c in commitments),
        missed=sum(c.status == "missed" for c in commitments) + len(overdue),
        open=sum(c.status == "open" for c in commitments) - len(overdue),
        drift_minutes=manager.drift_minutes(start, min(end, now)),
        answered=sum(1 for c in calls if c.answered_at),
        dodged=sum(c.status == "missed" for c in calls),
    )


def week(manager: CallManager, days: int = 7) -> list[DayScore]:
    now = manager.clock()
    today = local_date(now, manager.db.get_settings())
    return [score_day(manager, today - timedelta(days=i), now) for i in range(days)]
