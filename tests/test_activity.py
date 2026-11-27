from datetime import timedelta

from nagme.activity import RawSample, domain_of, is_distracted
from tests.conftest import turn_on

RULES = ["youtube.com", "x.com", "messages"]


def test_domain_and_distraction_rules():
    assert domain_of("https://www.youtube.com/watch?v=1") == "youtube.com"
    assert domain_of("https://m.youtube.com/") == "m.youtube.com"
    assert domain_of(None) is None
    assert is_distracted("Google Chrome", "m.youtube.com", RULES)
    assert is_distracted("Messages", None, RULES)
    assert not is_distracted("Google Chrome", "box.com", RULES)  # suffix, not substring
    assert not is_distracted("Code", None, RULES)


async def advance(watcher, clock, sampler, minutes, sample):
    sampler.next = sample
    for _ in range(int(minutes * 3)):  # one sample every 20 seconds
        await watcher.step()
        clock.now += timedelta(seconds=20)


async def test_drift_warns_then_calls(manager, watcher, sampler, clock, notes):
    turn_on(manager, morning_call=False, review_call=False, drift_minutes=10)
    manager.add_commitment("Draft memo", due_in_minutes=120)
    youtube = RawSample("Google Chrome", "https://www.youtube.com/watch?v=x", 0)

    await advance(watcher, clock, sampler, 9, youtube)
    assert notes == []  # not enough history yet
    await advance(watcher, clock, sampler, 2, youtube)
    assert len(notes) == 1 and "youtube.com" in notes[0][1] and "Calling in 3 min" in notes[0][1]
    assert manager.db.pending_calls() == []

    await advance(watcher, clock, sampler, 3.4, youtube)
    [call] = manager.db.pending_calls()
    assert call.purpose == "drift"
    assert "youtube.com" in call.reason and "Draft memo" in call.reason


async def test_getting_back_to_work_cancels_the_warning(manager, watcher, sampler, clock, notes):
    turn_on(manager, morning_call=False, review_call=False, drift_minutes=10)
    await advance(watcher, clock, sampler, 11, RawSample("Google Chrome", "https://x.com/", 0))
    assert len(notes) == 1
    await advance(watcher, clock, sampler, 4, RawSample("Code", None, 0))
    assert manager.db.pending_calls() == []
    assert watcher.warned_at is None


async def test_idle_is_not_drift_and_nothing_recorded_off_hours(
    manager, watcher, sampler, clock, notes
):
    turn_on(manager, morning_call=False, review_call=False, drift_minutes=10)
    await advance(watcher, clock, sampler, 12, RawSample("Google Chrome", "https://x.com/", 600))
    assert notes == []
    clock.now = clock.now.replace(hour=2)  # 8pm Chicago the evening before
    before = len(manager.db.samples_between(clock.now - timedelta(days=1), clock.now))
    await advance(watcher, clock, sampler, 1, RawSample("Code", None, 0))
    after = len(manager.db.samples_between(clock.now - timedelta(days=1), clock.now))
    assert after == before


async def test_drift_minutes_feed_the_scorecard(manager, watcher, sampler, clock):
    turn_on(manager, morning_call=False, review_call=False, drift_minutes=30)
    await advance(watcher, clock, sampler, 6, RawSample("Google Chrome", "https://x.com/", 0))
    await advance(watcher, clock, sampler, 6, RawSample("Code", None, 0))
    assert manager.drift_minutes(clock.now - timedelta(hours=1), clock.now) == 6


async def test_paused_drift_warns_once_not_every_few_minutes(
    manager, watcher, sampler, clock, notes
):
    turn_on(manager, morning_call=False, review_call=False, drift_minutes=10)
    s = manager.db.get_settings()
    s.active = False
    manager.apply_settings(s)
    await advance(watcher, clock, sampler, 25, RawSample("Google Chrome", "https://x.com/", 0))
    assert len(notes) == 1
    assert "Calling" not in notes[0][1]
    assert manager.db.pending_calls() == []
