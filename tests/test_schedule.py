import random
from datetime import UTC, datetime, time, timedelta

from nagme.db import Settings
from nagme.schedule import in_work_hours, next_call_time

CHI = Settings(timezone="America/Chicago", start_time=time(9), end_time=time(17))


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_mid_day_gap_stays_within_jitter():
    now = utc(2026, 11, 24, 16, 0)  # Tue 10:00 Chicago
    for seed in range(50):
        nxt = next_call_time(now, CHI, random.Random(seed))
        assert timedelta(minutes=42) <= nxt - now <= timedelta(minutes=78)


def test_near_end_of_day_rolls_to_next_morning():
    now = utc(2026, 11, 24, 22, 40)  # Tue 16:40 Chicago, 20 min before work ends
    nxt = next_call_time(now, CHI, random.Random(1))
    # Wed 09:02-09:15 Chicago = 15:02-15:15 UTC
    assert utc(2026, 11, 25, 15, 2) <= nxt <= utc(2026, 11, 25, 15, 15)


def test_before_work_starts_calls_shortly_after_start():
    now = utc(2026, 11, 24, 12, 0)  # Tue 06:00 Chicago
    nxt = next_call_time(now, CHI, random.Random(2))
    assert utc(2026, 11, 24, 15, 2) <= nxt <= utc(2026, 11, 24, 15, 15)


def test_friday_evening_skips_the_weekend():
    now = utc(2026, 11, 28, 1, 0)  # Fri 19:00 Chicago
    nxt = next_call_time(now, CHI, random.Random(3))
    assert nxt.astimezone(CHI_TZ()).strftime("%a") == "Mon"


def test_daylight_saving_start_keeps_local_work_hours():
    # Chicago springs forward on Sun 2026-03-08. Monday 09:00 is then 14:00 UTC, not 15:00.
    now = utc(2026, 3, 7, 1, 0)  # Fri 19:00 CST
    nxt = next_call_time(now, CHI, random.Random(4))
    assert utc(2026, 3, 9, 14, 2) <= nxt <= utc(2026, 3, 9, 14, 15)


def test_no_work_days_means_no_calls():
    s = Settings(work_days=())
    assert next_call_time(utc(2026, 11, 24, 16, 0), s) is None


def test_in_work_hours():
    assert in_work_hours(utc(2026, 11, 24, 16, 0), CHI)
    assert not in_work_hours(utc(2026, 11, 24, 23, 30), CHI)  # 17:30
    assert not in_work_hours(utc(2026, 11, 29, 16, 0), CHI)  # Sunday


def CHI_TZ():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/Chicago")
