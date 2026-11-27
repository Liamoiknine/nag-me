"""The call lifecycle and the commitments that drive it.

Nag Me never calls on a timer. A call happens for a reason:
- morning:  just after work starts, if there's no plan for today yet
- deadline: something you committed to is due
- drift:    the Mac watcher saw you off task for a while (see activity.py)
- review:   shortly before work ends
- manual / inbound: you asked for it ("Call me now", or you called the number)
Missed calls are retried a few minutes later, a limited number of times.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import coach
from .config import Config
from .db import FINAL_STATUSES, Call, Commitment, Database, Settings, utcnow
from .schedule import (
    day_bounds,
    in_work_hours,
    local_date,
    morning_call_time,
    review_call_time,
    work_window,
)
from .telephony import Telephony

log = logging.getLogger("nagme.calls")

TICK_SECONDS = 15
SAMPLE_SECONDS = 20  # how often the Mac watcher samples; one sample = this much time
BUSY_BACKOFF = timedelta(minutes=2)  # a due call waits this long if another is live
DIAL_TIMEOUT = timedelta(minutes=3)  # dialing with no word from Twilio means a broken setup
INBOUND_TIMEOUT = timedelta(seconds=45)  # you called but the audio never connected
MORNING_CUTOFF = timedelta(hours=4)
TWILIO_FINAL = {"completed", "busy", "no-answer", "failed", "canceled"}
ON_DEMAND = ("manual", "inbound")

Notifier = Callable[[str, str], None]  # (title, message)


class CallManager:
    def __init__(
        self,
        db: Database,
        cfg: Config,
        telephony: Telephony,
        *,
        clock: Callable[[], datetime] = utcnow,
        notify: Notifier | None = None,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.telephony = telephony
        self.clock = clock
        self.notify: Notifier = notify or (lambda title, message: None)

    # Scheduling

    async def run_forever(self) -> None:
        log.info("Scheduler running (checks every %ss)", TICK_SECONDS)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        now = self.clock()
        s = self.db.get_settings()
        self.expire_stale_calls(now)
        if s.active and s.phone_number:
            self._queue_routine_calls(now, s)
            self._queue_deadline_calls(now)

        snoozed = s.snoozed_until is not None and now < s.snoozed_until
        for call in self.db.due_calls(now):
            if call.purpose not in ON_DEMAND:
                verdict = self._check_automatic(call, now, s)
                if verdict == "hold" or (verdict is None and snoozed):
                    continue  # stays pending: snoozed, or waiting for work hours
                if verdict is not None:
                    self.db.update_call(call.id, status="skipped", error=verdict)
                    continue
            if self.db.open_calls():
                self.db.update_call(call.id, scheduled_for=now + BUSY_BACKOFF)
                continue
            await self.place(call, s)

    def _check_automatic(self, call: Call, now: datetime, s: Settings) -> str | None:
        """None to dial now, "hold" to wait, or the reason to skip the call for good."""
        if not s.active:
            return "Calls are paused"
        if call.purpose == "deadline":
            if not any(c.due_at <= now for c in self.db.open_commitments()):
                return "Nothing due anymore"
            morning = self._morning_state(now, s)
            if morning == "active":
                return "Covered by the morning call"
            if morning == "upcoming" or not in_work_hours(now, s):
                return "hold"  # deadlines outside work hours wait for the next work window
            return None
        # Morning, review and drift calls (and their retries) belong to the day and the
        # hours they were made for; after a sleep or a long snooze they're stale.
        if local_date(call.scheduled_for, s) != local_date(now, s):
            return "From an earlier day"
        if not in_work_hours(now, s):
            return "Outside work hours"
        return None

    def _morning_state(self, now: datetime, s: Settings) -> str | None:
        """'active' if today's morning call is queued or live, 'upcoming' if it will be."""
        today = local_date(now, s)
        window = work_window(today, s)
        if not s.morning_call or window is None or self.db.get_plan(today) is not None:
            return None
        mornings = [
            c for c in self.db.calls_between(*day_bounds(today, s)) if c.purpose == "morning"
        ]
        if any(c.status in ("pending", "dialing", "answered") for c in mornings):
            return "active"
        if not mornings and now < morning_call_time(window):
            return "upcoming"
        return None

    def _queue_routine_calls(self, now: datetime, s: Settings) -> None:
        today = local_date(now, s)
        window = work_window(today, s)
        if window is None or now >= window[1]:
            return
        today_calls = self.db.calls_between(*day_bounds(today, s))
        purposes = {c.purpose for c in today_calls}
        if (
            s.morning_call
            and morning_call_time(window) <= now < self._morning_cutoff(window)
            and "morning" not in purposes
            and self.db.get_plan(today) is None
        ):
            self.db.create_call("morning", now)
            log.info("Queued the morning planning call")
        if s.review_call and now >= review_call_time(window) and "review" not in purposes:
            self.db.create_call("review", now)
            log.info("Queued the end-of-day review call")

    @staticmethod
    def _morning_cutoff(window: tuple[datetime, datetime]) -> datetime:
        """No "morning" planning call after this; a late start just skips it."""
        return min(window[0] + MORNING_CUTOFF, review_call_time(window))

    def _queue_deadline_calls(self, now: datetime) -> None:
        due = self.db.unchecked_due_commitments(now)
        if not due:
            return
        for commitment in due:
            self.db.update_commitment(commitment.id, checked_at=now)
        if not any(c.purpose == "deadline" for c in self.db.pending_calls()):
            self.db.create_call("deadline", now)
            log.info("Queued a deadline call for: %s", ", ".join(c.text for c in due))

    def queue_drift_call(self, reason: str) -> Call | None:
        s = self.db.get_settings()
        now = self.clock()
        if not (s.active and s.phone_number) or self.db.open_calls():
            return None
        if any(c.purpose == "drift" for c in self.db.pending_calls()):
            return None
        log.info("Queued a drift call: %s", reason)
        return self.db.create_call("drift", now, reason=reason)

    def apply_settings(self, s: Settings) -> None:
        if not s.active:
            for call in self.db.pending_calls():
                self.db.update_call(call.id, status="skipped", error="Calls paused")
        self.db.save_settings(s)

    def snooze(self, minutes: int) -> datetime:
        s = self.db.get_settings()
        s.snoozed_until = self.clock() + timedelta(minutes=max(1, min(240, minutes)))
        self.db.save_settings(s)
        return s.snoozed_until

    def last_drift_call_at(self) -> datetime | None:
        now = self.clock()
        calls = self.db.calls_between(now - timedelta(days=1), now + timedelta(minutes=1))
        drift = [c.scheduled_for for c in calls if c.purpose == "drift"]
        return max(drift) if drift else None

    # Dialing

    async def call_now(self, purpose: str = "manual") -> Call:
        if self.db.open_calls():
            raise RuntimeError("A call is already in progress.")
        s = self.db.get_settings()
        if not s.phone_number:
            raise RuntimeError("Add your phone number in Settings first.")
        call = self.db.create_call(purpose, self.clock())
        await self.place(call, s)
        return self.db.get_call(call.id) or call

    async def place(self, call: Call, s: Settings) -> None:
        token = secrets.token_urlsafe(24)
        if not self.db.claim_call(call.id, token, self.clock()):
            return  # skipped or claimed elsewhere (e.g. paused from the menu bar) meanwhile
        try:
            sid = await asyncio.to_thread(self.telephony.place_call, s.phone_number, call.id)
        except Exception as exc:
            log.warning("Could not place call %s: %s", call.id, exc)
            self.db.update_call(call.id, status="failed", error=str(exc)[:500])
            return
        self.db.update_call(call.id, twilio_sid=sid)
        log.info("Dialing %s call %s (%s)", call.purpose, call.id, sid)

    def start_inbound(self, twilio_sid: str) -> Call:
        """You called the Nag Me number: open a call record the media stream can claim."""
        now = self.clock()
        call = self.db.create_call("inbound", now)
        token = secrets.token_urlsafe(24)
        self.db.update_call(
            call.id, status="dialing", stream_token=token, dialed_at=now, twilio_sid=twilio_sid
        )
        return self.db.get_call(call.id) or call

    async def hang_up(self, call_sid: str | None) -> None:
        if not call_sid:
            return
        try:
            await asyncio.to_thread(self.telephony.hang_up, call_sid)
        except Exception:
            log.exception("Hang-up failed for %s", call_sid)

    # Twilio status callbacks

    def handle_status(self, call_id: int, twilio_status: str) -> None:
        if twilio_status not in TWILIO_FINAL:
            return
        call = self.db.get_call(call_id)
        if call is None or call.status in FINAL_STATUSES:
            return
        now = self.clock()
        if call.answered_at:
            self.db.update_call(call.id, status="completed", ended_at=call.ended_at or now)
        elif twilio_status == "failed":
            self.db.update_call(
                call.id,
                status="failed",
                ended_at=now,
                error="Twilio couldn't place the call. On a trial account, the number "
                "must be a verified caller ID.",
            )
        else:
            # Busy, no answer, declined, voicemail or call screening (on a trial account,
            # answered without pressing a key): either way, you dodged it.
            self.db.update_call(call.id, status="missed", ended_at=now)
            self._schedule_retry(call, now)

    def handle_machine(self, call_id: int) -> None:
        """Voicemail or a screening service picked up: that's a dodge, not an answer."""
        call = self.db.get_call(call_id)
        if call is None or call.status in FINAL_STATUSES:
            return
        now = self.clock()
        self.db.update_call(call.id, status="missed", ended_at=now, error="Voicemail answered")
        self._schedule_retry(call, now)

    def _schedule_retry(self, call: Call, now: datetime) -> None:
        s = self.db.get_settings()
        if call.purpose in ON_DEMAND or not s.active or call.attempt >= s.max_retries:
            return
        retry_at = now + timedelta(minutes=s.retry_delay_minutes)
        self.db.create_call(call.purpose, retry_at, attempt=call.attempt + 1, reason=call.reason)
        log.info("Call %s was missed; retrying at %s", call.id, retry_at)

    def expire_stale_calls(self, now: datetime | None = None) -> None:
        now = now or self.clock()
        for call in self.db.open_calls():
            age = now - call.dialed_at if call.dialed_at else timedelta(0)
            if call.status == "dialing" and call.purpose == "inbound" and age > INBOUND_TIMEOUT:
                self.db.update_call(call.id, status="skipped", error="Hung up before connecting")
            elif call.status == "dialing" and call.purpose != "inbound" and age > DIAL_TIMEOUT:
                self.db.update_call(
                    call.id,
                    status="failed",
                    error="No status from Twilio. Is PUBLIC_URL reachable from the internet?",
                )
            elif (
                call.status == "answered"
                and call.answered_at
                and now - call.answered_at > timedelta(seconds=self.cfg.max_call_seconds + 120)
            ):
                self.db.update_call(call.id, status="completed", ended_at=call.ended_at or now)

    # Media stream bookkeeping

    def authorize_stream(self, call_id: str | None, token: str | None) -> Call | None:
        """Accept a media stream only for a call we're dialing, with its one-time token."""
        if not call_id or not token or not call_id.isdigit():
            return None
        call = self.db.get_call(int(call_id))
        if (
            call is None
            or call.status != "dialing"
            or not call.stream_token
            or not hmac.compare_digest(token, call.stream_token)
        ):
            return None
        now = self.clock()
        self.db.update_call(call.id, status="answered", answered_at=now)
        call.status, call.answered_at = "answered", now
        return call

    def finish_stream(self, call_id: int, transcript: list[dict[str, str]], error: str | None):
        values: dict = {"status": "completed", "ended_at": self.clock(), "transcript": transcript}
        if error:
            values["error"] = error[:500]
        self.db.update_call(call_id, **values)

    # Coach tools

    def add_commitment(
        self,
        text: str,
        due_in_minutes: object = None,
        due_time: object = None,
        source: str = "call",
    ) -> Commitment:
        text = str(text or "").strip()[:200]
        if due_in_minutes not in (None, "") and not str(due_in_minutes).lstrip("-").isdigit():
            raise ValueError("due_in_minutes must be a whole number.")
        if not text:
            raise ValueError("Say what the commitment is.")
        now = self.clock()
        s = self.db.get_settings()
        low, high = coach.DUE_RANGE
        if due_in_minutes not in (None, ""):
            minutes = max(low, min(high, int(due_in_minutes)))  # type: ignore[call-overload]
            due = now + timedelta(minutes=minutes)
        elif due_time:
            tz = ZoneInfo(s.timezone)
            try:
                at = time.fromisoformat(str(due_time).strip())
            except ValueError:
                raise ValueError("due_time must look like 14:30.") from None
            day = local_date(now, s)
            due = datetime.combine(day, at, tz)
            if due <= now:
                # Already past today: the next work day at that time ("tomorrow morning"
                # said on a Friday means Monday).
                day += timedelta(days=1)
                for _ in range(6):
                    if day.weekday() in s.work_days:
                        break
                    day += timedelta(days=1)
                due = datetime.combine(day, at, tz)
        else:
            raise ValueError("Give due_in_minutes or due_time.")
        commitment = self.db.add_commitment(text, due, source)
        log.info("Commitment %s: %s, due %s", commitment.id, text, due)
        return commitment

    def update_commitment(
        self, commitment_id: object, status: str, new_due_in_minutes: object = None
    ) -> Commitment:
        found = self.db.get_commitment(int(commitment_id))  # type: ignore[call-overload]
        if found is None:
            raise ValueError(f"No commitment with id {commitment_id}.")
        now = self.clock()
        if status == "done":
            self.db.update_commitment(found.id, status="done", resolved_at=now)
        elif status == "dropped":
            self.db.update_commitment(found.id, status="dropped", resolved_at=now)
        elif status == "not_done" and new_due_in_minutes not in (None, ""):
            low, high = coach.DUE_RANGE
            minutes = max(low, min(high, int(new_due_in_minutes)))  # type: ignore[call-overload]
            self.db.update_commitment(
                found.id, status="open", due_at=now + timedelta(minutes=minutes), checked_at=None
            )
        elif status == "not_done":
            self.db.update_commitment(found.id, status="missed", resolved_at=now)
        else:
            raise ValueError(f"Unknown status {status!r}.")
        updated = self.db.get_commitment(found.id)
        assert updated is not None
        return updated

    def save_outcome(self, call_id: int, summary: str, on_track: str, plan: str | None) -> None:
        if on_track not in ("yes", "no", "unclear"):
            on_track = "unclear"
        self.db.update_call(call_id, summary=summary.strip()[:500] or None, on_track=on_track)
        plan = (plan or "").strip()[:500]
        if plan:
            self.db.set_plan(local_date(self.clock(), self.db.get_settings()), plan)

    def local_time(self, dt: datetime) -> str:
        tz = ZoneInfo(self.db.get_settings().timezone)
        local = dt.astimezone(tz)
        day = "" if local.date() == self.clock().astimezone(tz).date() else f"{local:%a} "
        return day + coach.clock(local)

    # Context for the coach

    def drift_minutes(self, start: datetime, end: datetime) -> int:
        samples = self.db.samples_between(start, end)
        return round(sum(1 for x in samples if x.distracted and not x.idle) * SAMPLE_SECONDS / 60)

    def todays_commitments(self) -> list[Commitment]:
        s = self.db.get_settings()
        start, end = day_bounds(local_date(self.clock(), s), s)
        today = self.db.commitments_between(start, end)
        open_items = self.db.open_commitments()
        carried = [c for c in open_items if c.due_at < start]
        upcoming = [c for c in open_items if c.due_at >= end]
        return carried + today + upcoming

    def call_context(self, call: Call) -> coach.CallContext:
        s = self.db.get_settings()
        tz = ZoneInfo(s.timezone)
        now = self.clock()
        today = local_date(now, s)
        start, end = day_bounds(today, s)
        calls = [c for c in self.db.calls_between(start, end) if c.id != call.id]

        def view(c: Commitment) -> coach.CommitmentView:
            return coach.CommitmentView(c.id, c.text, c.due_at.astimezone(tz), c.status)

        commitments = self.todays_commitments()
        return coach.CallContext(
            purpose=call.purpose,
            style=s.coach_style,
            now=now.astimezone(tz),
            plan=self.db.get_plan(today),
            commitments=[view(c) for c in commitments],
            due_now=[view(c) for c in commitments if c.status == "open" and c.due_at <= now],
            earlier=[
                coach.EarlierCall(c.answered_at.astimezone(tz), c.summary, c.on_track)
                for c in calls
                if c.summary and c.answered_at
            ][-5:],
            missed_today=sum(c.status == "missed" for c in calls),
            drift_reason=call.reason if call.purpose == "drift" else None,
            drift_minutes_today=self.drift_minutes(start, now) if s.drift_enabled else None,
            calls_on=s.active,
        )
