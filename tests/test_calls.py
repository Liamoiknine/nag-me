import asyncio
from datetime import UTC, datetime, timedelta

from nagme.coach import build_instructions, greeting, tools
from nagme.schedule import local_date
from tests.conftest import PHONE, turn_on


def run(coro):
    return asyncio.run(coro)


def placed_purposes(manager, telephony):
    return [manager.db.get_call(call_id).purpose for _, call_id in telephony.placed]


def test_nothing_happens_while_paused(manager, telephony):
    s = manager.db.get_settings()
    s.phone_number = PHONE
    manager.apply_settings(s)
    run(manager.tick())
    assert telephony.placed == []


def test_morning_call_only_without_a_plan(manager, telephony, clock):
    turn_on(manager, review_call=False)
    run(manager.tick())
    assert placed_purposes(manager, telephony) == ["morning"]
    morning = manager.db.get_call(1)
    manager.authorize_stream(str(morning.id), morning.stream_token)
    manager.finish_stream(morning.id, [], None)
    clock.now += timedelta(minutes=30)
    run(manager.tick())
    assert len(telephony.placed) == 1  # once per day


def test_no_morning_call_if_plan_already_set(manager, telephony, clock):
    turn_on(manager, review_call=False)
    manager.db.set_plan(local_date(clock.now, manager.db.get_settings()), "Ship the deck")
    run(manager.tick())
    assert telephony.placed == []


def test_review_call_before_work_ends(manager, telephony, clock):
    turn_on(manager, morning_call=False)
    run(manager.tick())
    assert telephony.placed == []
    clock.now = datetime(2026, 11, 24, 22, 46, tzinfo=UTC)  # 16:46 Chicago
    run(manager.tick())
    assert placed_purposes(manager, telephony) == ["review"]


def test_commitment_coming_due_triggers_one_deadline_call(manager, telephony, clock):
    turn_on(manager, morning_call=False, review_call=False)
    manager.add_commitment("Draft memo", due_in_minutes=30)
    manager.add_commitment("Email Sam", due_in_minutes=31)
    clock.now += timedelta(minutes=29)
    run(manager.tick())
    assert telephony.placed == []
    clock.now += timedelta(minutes=3)
    run(manager.tick())
    assert placed_purposes(manager, telephony) == ["deadline"]  # one call covers both
    call = manager.db.get_call(telephony.placed[0][1])
    ctx = manager.call_context(call)
    assert [c.text for c in ctx.due_now] == ["Draft memo", "Email Sam"]
    assert "whether they finished: Draft memo" in greeting(ctx)
    run(manager.tick())
    assert len(telephony.placed) == 1


def test_not_done_with_new_time_reopens_and_calls_again(manager, telephony, clock):
    turn_on(manager, morning_call=False, review_call=False)
    c = manager.add_commitment("Draft memo", due_in_minutes=10)
    clock.now += timedelta(minutes=11)
    run(manager.tick())
    manager.handle_status(telephony.placed[0][1], "completed")
    manager.update_commitment(c.id, "not_done", new_due_in_minutes=20)
    clock.now += timedelta(minutes=21)
    run(manager.tick())
    assert placed_purposes(manager, telephony) == ["deadline", "deadline"]
    manager.update_commitment(c.id, "done")
    assert manager.db.get_commitment(c.id).status == "done"


def test_due_time_already_past_means_tomorrow(manager, clock):
    turn_on(manager)
    c = manager.add_commitment("Gym", due_time="08:00")  # it's 10:00 now
    assert c.due_at == datetime(2026, 11, 25, 14, 0, tzinfo=UTC)
    c = manager.add_commitment("Lunch", due_time="12:30")
    assert c.due_at == datetime(2026, 11, 24, 18, 30, tzinfo=UTC)


def test_missed_call_is_retried_then_gives_up(manager, telephony, clock):
    turn_on(manager, review_call=False)
    run(manager.tick())
    manager.handle_status(telephony.placed[-1][1], "no-answer")
    for attempt in (1, 2):
        clock.now += timedelta(minutes=5)
        run(manager.tick())
        retry = manager.db.get_call(telephony.placed[-1][1])
        assert retry.purpose == "morning" and retry.attempt == attempt
        manager.handle_status(retry.id, "busy")
    clock.now += timedelta(minutes=5)
    run(manager.tick())
    assert len(telephony.placed) == 3  # max_retries is 2


def test_manual_and_failed_calls_are_not_retried(manager, telephony):
    turn_on(manager, morning_call=False, review_call=False)
    call = run(manager.call_now())
    manager.handle_status(call.id, "no-answer")
    telephony.fail_with = RuntimeError("The number is unverified.")
    failed = run(manager.call_now())
    assert failed.status == "failed" and "unverified" in failed.error
    assert manager.db.pending_calls() == []


def test_snooze_holds_automatic_calls_but_not_manual_ones(manager, telephony, clock):
    turn_on(manager, review_call=False)
    manager.snooze(30)
    run(manager.tick())
    assert telephony.placed == []
    run(manager.call_now())
    assert placed_purposes(manager, telephony) == ["manual"]
    manager.db.update_call(telephony.placed[0][1], status="completed")
    clock.now += timedelta(minutes=31)
    run(manager.tick())
    assert placed_purposes(manager, telephony) == ["manual", "morning"]


def test_drift_call_outside_work_hours_is_skipped(manager, telephony, clock):
    turn_on(manager, morning_call=False, review_call=False)
    clock.now = datetime(2026, 11, 25, 1, 0, tzinfo=UTC)  # 7pm Chicago
    call = manager.queue_drift_call("Mostly on youtube.com")
    run(manager.tick())
    assert manager.db.get_call(call.id).status == "skipped"
    assert telephony.placed == []


def test_pausing_cancels_pending_calls(manager, clock):
    turn_on(manager, review_call=False)
    manager.db.create_call("deadline", clock.now + timedelta(minutes=5))
    s = manager.db.get_settings()
    s.active = False
    manager.apply_settings(s)
    assert manager.db.pending_calls() == []


def test_stream_token_is_single_use(manager):
    turn_on(manager)
    call = run(manager.call_now())
    assert manager.authorize_stream(str(call.id), "wrong") is None
    assert manager.authorize_stream(str(call.id), call.stream_token) is not None
    assert manager.authorize_stream(str(call.id), call.stream_token) is None


def test_stale_calls_expire(manager, clock):
    turn_on(manager, morning_call=False, review_call=False)
    out = run(manager.call_now())
    manager.handle_status(out.id, "completed")  # never answered: dodged
    dialing = run(manager.call_now())
    inbound = manager.start_inbound("CA123")
    clock.now += timedelta(minutes=4)
    run(manager.tick())
    assert manager.db.get_call(dialing.id).status == "failed"
    assert "PUBLIC_URL" in manager.db.get_call(dialing.id).error
    assert manager.db.get_call(inbound.id).status == "skipped"


def test_context_and_instructions(manager, clock):
    turn_on(manager, morning_call=False, review_call=False)
    done = run(manager.call_now())
    manager.authorize_stream(str(done.id), done.stream_token)
    manager.save_outcome(done.id, "Writing the essay intro.", "yes", "Finish essay draft.")
    manager.finish_stream(done.id, [], None)
    manager.add_commitment("Essay intro", due_in_minutes=5)
    missed = manager.db.create_call("deadline", clock.now)
    manager.db.update_call(missed.id, status="missed")

    clock.now += timedelta(minutes=6)
    current = manager.db.create_call("deadline", clock.now, attempt=1)
    ctx = manager.call_context(current)
    assert ctx.plan == "Finish essay draft." and not ctx.needs_plan
    assert ctx.missed_today == 1
    text = build_instructions(ctx)
    for expected in ("Finish essay draft.", "Essay intro", "Writing the essay intro.",
                     "didn't pick up 1 earlier call", "update_commitment"):  # fmt: skip
        assert expected in text


def test_plan_is_required_on_the_morning_call(manager, clock):
    turn_on(manager)
    call = manager.db.create_call("morning", clock.now)
    ctx = manager.call_context(call)
    finish = next(t for t in tools(ctx) if t["name"] == "finish_call")
    assert "plan" in finish["parameters"]["required"]
    s = manager.db.get_settings()
    s.active = False
    manager.apply_settings(s)
    text = build_instructions(manager.call_context(call))
    assert "will NOT call them back" in text and "never offer to reschedule" not in text


# Timing guards found in review


def chicago(day, hour, minute=0):
    from zoneinfo import ZoneInfo

    return datetime(2026, 11, day, hour, minute, tzinfo=ZoneInfo("America/Chicago")).astimezone(UTC)


def test_plan_is_keyed_by_local_date_late_in_the_evening(manager, clock):
    turn_on(manager)
    clock.now = chicago(24, 21)  # 9pm Tuesday in Chicago is already Wednesday in UTC
    call = manager.db.create_call("manual", clock.now)
    manager.save_outcome(call.id, "x", "yes", "Evening plan")
    from datetime import date

    assert manager.db.get_plan(date(2026, 11, 24)) == "Evening plan"
    assert manager.db.get_plan(date(2026, 11, 25)) is None


def test_yesterdays_pending_review_does_not_ring_next_morning(manager, telephony, clock):
    turn_on(manager, morning_call=False)
    clock.now = chicago(24, 16, 45)
    manager.db.create_call("review", clock.now)  # queued, then the Mac slept
    clock.now = chicago(25, 9, 30)
    run(manager.tick())
    assert telephony.placed == []


def test_snoozing_past_the_end_of_work_drops_the_review(manager, telephony, clock):
    turn_on(manager, morning_call=False)
    clock.now = chicago(24, 16, 46)
    manager.snooze(240)
    run(manager.tick())
    clock.now = chicago(24, 20, 50)
    run(manager.tick())
    assert telephony.placed == []


def test_no_morning_call_late_in_the_day(manager, telephony, clock):
    turn_on(manager, review_call=False)
    clock.now = chicago(24, 16, 20)
    run(manager.tick())
    assert telephony.placed == []


def test_friday_tomorrow_means_monday(manager, clock):
    turn_on(manager)
    clock.now = chicago(27, 16, 50)  # Friday
    c = manager.add_commitment("Write the brief", due_time="09:00")
    assert c.due_at == chicago(30, 9, 0)  # Monday


def test_after_hours_deadline_waits_and_folds_into_the_morning_call(manager, telephony, clock):
    turn_on(manager, review_call=False)
    clock.now = chicago(24, 18, 0)
    manager.add_commitment("Read the contract", due_in_minutes=30)
    clock.now = chicago(24, 18, 31)
    run(manager.tick())
    assert telephony.placed == []  # after hours: held
    clock.now = chicago(25, 9, 1)
    run(manager.tick())
    assert telephony.placed == []  # morning call is about to come
    clock.now = chicago(25, 9, 3)
    run(manager.tick())
    assert placed_purposes(manager, telephony) == ["morning"]
    ctx = manager.call_context(manager.db.get_call(telephony.placed[0][1]))
    assert [c.text for c in ctx.due_now] == ["Read the contract"]


def test_deadline_call_skipped_when_nothing_is_due_anymore(manager, telephony, clock):
    turn_on(manager, morning_call=False, review_call=False)
    c = manager.add_commitment("Memo", due_in_minutes=10)
    manager.snooze(30)
    clock.now += timedelta(minutes=11)
    run(manager.tick())
    manager.update_commitment(c.id, "done")  # texted "done with memo" while snoozed
    clock.now += timedelta(minutes=20)
    run(manager.tick())
    assert telephony.placed == []


def test_a_call_skipped_meanwhile_is_not_dialed(manager, telephony, clock):
    turn_on(manager)
    call = manager.db.create_call("manual", clock.now)
    manager.db.update_call(call.id, status="skipped")  # e.g. paused from the menu bar
    run(manager.place(call, manager.db.get_settings()))
    assert telephony.placed == []


def test_voicemail_counts_as_dodged_and_retries(manager, clock):
    turn_on(manager, review_call=False)
    run(manager.tick())
    manager.handle_machine(1)
    assert manager.db.get_call(1).status == "missed"
    assert [c.purpose for c in manager.db.pending_calls()] == ["morning"]


def test_unconnected_inbound_call_stops_blocking_after_45_seconds(manager, clock):
    turn_on(manager)
    manager.start_inbound("CA1")
    clock.now += timedelta(seconds=50)
    manager.expire_stale_calls()
    assert manager.db.open_calls() == []


def test_bad_tool_arguments_raise_value_errors(manager):
    import pytest

    turn_on(manager)
    with pytest.raises(ValueError):
        manager.add_commitment("x", due_time="soon")
    with pytest.raises(ValueError):
        manager.add_commitment("x", due_in_minutes="a while")
