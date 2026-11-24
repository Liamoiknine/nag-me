import asyncio
from datetime import timedelta

from nagme.coach import CallContext, build_instructions

PHONE = "+13145550100"


def turn_on(manager):
    s = manager.db.get_settings()
    s.phone_number, s.active = PHONE, True
    manager.apply_settings(s)
    return manager.db.get_settings()


def run(coro):
    return asyncio.run(coro)


def test_scheduled_call_is_placed_when_due(manager, telephony, clock):
    s = turn_on(manager)
    assert s.next_call_at > clock.now
    clock.now = s.next_call_at + timedelta(seconds=1)
    run(manager.tick())
    assert telephony.placed == [(PHONE, 1)]
    call = manager.db.get_call(1)
    assert call.status == "dialing" and call.stream_token
    assert manager.db.get_settings().next_call_at > clock.now


def test_missed_call_is_retried_then_gives_up(manager, telephony, clock):
    turn_on(manager)
    first = manager.db.create_call("scheduled", clock.now)
    run(manager.place(first, manager.db.get_settings()))
    manager.handle_status(first.id, "no-answer")
    assert manager.db.get_call(first.id).status == "missed"

    for attempt in (1, 2):
        clock.now += timedelta(minutes=5)
        run(manager.tick())
        retry = manager.db.get_call(telephony.placed[-1][1])
        assert retry.kind == "retry" and retry.attempt == attempt
        manager.handle_status(retry.id, "busy")

    clock.now += timedelta(minutes=5)
    before = len(telephony.placed)
    run(manager.tick())
    assert len(telephony.placed) == before  # max_retries is 2


def test_retry_after_work_hours_is_skipped(manager, telephony, clock):
    turn_on(manager)
    clock.now = clock.now.replace(hour=22, minute=58)  # 16:58 Chicago
    call = manager.db.create_call("scheduled", clock.now)
    run(manager.place(call, manager.db.get_settings()))
    manager.handle_status(call.id, "no-answer")
    clock.now += timedelta(minutes=6)
    run(manager.tick())
    retry = manager.db.get_call(call.id + 1)
    assert retry.status == "skipped"
    assert len(telephony.placed) == 1


def test_failed_call_is_not_retried(manager, telephony, clock):
    turn_on(manager)
    call = manager.db.create_call("scheduled", clock.now)
    run(manager.place(call, manager.db.get_settings()))
    manager.handle_status(call.id, "failed")
    assert manager.db.get_call(call.id).status == "failed"
    assert manager.db.pending_calls() == []


def test_twilio_error_marks_call_failed(manager, telephony, clock):
    turn_on(manager)
    telephony.fail_with = RuntimeError("The number +13145550100 is unverified.")
    call = run(manager.call_now())
    assert call.status == "failed" and "unverified" in call.error


def test_answered_call_completes_and_status_is_idempotent(manager, clock):
    turn_on(manager)
    call = run(manager.call_now())
    assert manager.authorize_stream(str(call.id), call.stream_token).status == "answered"
    manager.handle_status(call.id, "completed")
    manager.handle_status(call.id, "no-answer")  # late duplicate is ignored
    assert manager.db.get_call(call.id).status == "completed"
    assert manager.db.pending_calls() == []


def test_stream_token_is_single_use(manager):
    turn_on(manager)
    call = run(manager.call_now())
    assert manager.authorize_stream(str(call.id), "wrong") is None
    assert manager.authorize_stream(str(call.id), call.stream_token) is not None
    assert manager.authorize_stream(str(call.id), call.stream_token) is None


def test_dialing_with_no_twilio_status_times_out(manager, clock):
    turn_on(manager)
    call = run(manager.call_now())
    clock.now += timedelta(minutes=4)
    run(manager.tick())
    call = manager.db.get_call(call.id)
    assert call.status == "failed" and "PUBLIC_URL" in call.error


def test_pausing_cancels_pending_retries(manager, clock):
    turn_on(manager)
    call = manager.db.create_call("scheduled", clock.now)
    run(manager.place(call, manager.db.get_settings()))
    manager.handle_status(call.id, "no-answer")
    s = manager.db.get_settings()
    s.active = False
    manager.apply_settings(s)
    assert manager.db.pending_calls() == []
    assert manager.db.get_settings().next_call_at is None


def test_context_carries_plan_summaries_and_dodges(manager, clock):
    turn_on(manager)
    done = run(manager.call_now())
    manager.authorize_stream(str(done.id), done.stream_token)
    manager.save_outcome(done.id, "Writing the essay intro.", "yes", "Finish essay draft by 4pm.")
    manager.finish_stream(done.id, [], None)
    missed = manager.db.create_call("scheduled", clock.now)
    run(manager.place(missed, manager.db.get_settings()))
    manager.handle_status(missed.id, "no-answer")

    current = manager.db.create_call("retry", clock.now, attempt=1)
    ctx = manager.call_context(current)
    assert not ctx.first_of_day
    assert ctx.plan == "Finish essay draft by 4pm."
    assert ctx.missed_today == 1
    text = build_instructions(ctx)
    assert "Finish essay draft by 4pm." in text
    assert "Writing the essay intro." in text
    assert "didn't pick up 1 call" in text


def test_first_call_of_day_asks_for_the_plan(clock):
    from datetime import datetime

    ctx = CallContext("strict", datetime(2026, 11, 24, 9, 5), True, None, [], 0)
    text = build_instructions(ctx)
    assert "first check-in today" in text
    assert "9:05 AM" in text
    assert "no-nonsense" in text
