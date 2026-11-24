"""When to call: pure functions over the work-hours settings.

Calls land at slightly random times (the average interval, plus or minus 30%) so you
can't predict the next one, and only inside your work hours in your own time zone.
All arithmetic happens in UTC, so daylight-saving changes can't shift a call.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Settings

JITTER = 0.3  # each gap is the interval times a random factor in [0.7, 1.3]
FIRST_CALL_DELAY = (2, 15)  # first call of the day comes 2-15 minutes after work starts


def work_window(day: date, s: Settings) -> tuple[datetime, datetime] | None:
    """The (start, end) of work on a local date, in UTC, or None on a day off."""
    if day.weekday() not in s.work_days:
        return None
    tz = ZoneInfo(s.timezone)
    start = datetime.combine(day, s.start_time, tz).astimezone(UTC)
    end = datetime.combine(day, s.end_time, tz).astimezone(UTC)
    return (start, end) if start < end else None


def in_work_hours(now: datetime, s: Settings) -> bool:
    window = work_window(now.astimezone(ZoneInfo(s.timezone)).date(), s)
    return window is not None and window[0] <= now < window[1]


def local_day_start(now: datetime, tz_name: str) -> datetime:
    """Midnight of the user's current local day, in UTC."""
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    return datetime.combine(local.date(), datetime.min.time(), tz).astimezone(UTC)


def _first_call(window: tuple[datetime, datetime], rng: random.Random) -> datetime:
    start, end = window
    latest = min(FIRST_CALL_DELAY[1], (end - start).total_seconds() / 120)
    return start + timedelta(minutes=rng.uniform(min(FIRST_CALL_DELAY[0], latest), latest))


def next_call_time(now: datetime, s: Settings, rng: random.Random | None = None) -> datetime | None:
    """The next scheduled call after `now`, or None if no work days are set."""
    rng = rng or random.Random()
    today = now.astimezone(ZoneInfo(s.timezone)).date()

    window = work_window(today, s)
    if window:
        start, end = window
        if now < start:
            return _first_call(window, rng)
        if now < end:
            gap = timedelta(minutes=s.interval_minutes * rng.uniform(1 - JITTER, 1 + JITTER))
            if now + gap < end:
                return now + gap

    for offset in range(1, 8):
        window = work_window(today + timedelta(days=offset), s)
        if window:
            return _first_call(window, rng)
    return None
