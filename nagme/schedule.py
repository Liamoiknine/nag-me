"""Time helpers over the work-hours settings. All arithmetic happens in UTC, so
daylight-saving changes can't shift a call."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Settings

MORNING_DELAY = timedelta(minutes=2)  # the plan call comes just after work starts
REVIEW_LEAD = timedelta(minutes=15)  # the review call comes this long before work ends


def local_date(now: datetime, s: Settings) -> date:
    return now.astimezone(ZoneInfo(s.timezone)).date()


def day_bounds(day: date, s: Settings) -> tuple[datetime, datetime]:
    """Midnight to midnight of a local date, in UTC."""
    tz = ZoneInfo(s.timezone)
    start = datetime.combine(day, datetime.min.time(), tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def work_window(day: date, s: Settings) -> tuple[datetime, datetime] | None:
    """The (start, end) of work on a local date, in UTC, or None on a day off."""
    if day.weekday() not in s.work_days:
        return None
    tz = ZoneInfo(s.timezone)
    start = datetime.combine(day, s.start_time, tz).astimezone(UTC)
    end = datetime.combine(day, s.end_time, tz).astimezone(UTC)
    return (start, end) if start < end else None


def in_work_hours(now: datetime, s: Settings) -> bool:
    window = work_window(local_date(now, s), s)
    return window is not None and window[0] <= now < window[1]


def morning_call_time(window: tuple[datetime, datetime]) -> datetime:
    return window[0] + MORNING_DELAY


def review_call_time(window: tuple[datetime, datetime]) -> datetime:
    start, end = window
    return max(start + MORNING_DELAY + timedelta(minutes=1), end - REVIEW_LEAD)
