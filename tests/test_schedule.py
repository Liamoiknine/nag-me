from datetime import UTC, date, datetime, time

from nagme.db import Settings
from nagme.schedule import (
    day_bounds,
    in_work_hours,
    local_date,
    morning_call_time,
    review_call_time,
    work_window,
)

CHI = Settings(timezone="America/Chicago", start_time=time(9), end_time=time(17))


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_work_window_in_utc():
    assert work_window(date(2026, 11, 24), CHI) == (utc(2026, 11, 24, 15), utc(2026, 11, 24, 23))
    assert work_window(date(2026, 11, 29), CHI) is None  # Sunday


def test_daylight_saving_keeps_local_work_hours():
    # Chicago springs forward on Sun 2026-03-08: Monday 09:00 is 14:00 UTC, not 15:00.
    assert work_window(date(2026, 3, 9), CHI)[0] == utc(2026, 3, 9, 14)


def test_morning_and_review_times():
    window = work_window(date(2026, 11, 24), CHI)
    assert morning_call_time(window) == utc(2026, 11, 24, 15, 2)
    assert review_call_time(window) == utc(2026, 11, 24, 22, 45)


def test_in_work_hours_and_local_date():
    assert in_work_hours(utc(2026, 11, 24, 16), CHI)
    assert not in_work_hours(utc(2026, 11, 24, 23, 30), CHI)  # 17:30
    assert local_date(utc(2026, 11, 25, 3), CHI) == date(2026, 11, 24)  # 9pm Chicago
    start, end = day_bounds(date(2026, 11, 24), CHI)
    assert start == utc(2026, 11, 24, 6) and end == utc(2026, 11, 25, 6)
