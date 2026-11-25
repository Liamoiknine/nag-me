"""The call lifecycle: scheduling, dialing, retries when you dodge, and bookkeeping."""

from __future__ import annotations

import asyncio
import hmac
import logging
import random
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import coach
from .config import Config
from .db import FINAL_STATUSES, Call, Database, Settings, utcnow
from .schedule import in_work_hours, local_day_start, next_call_time
from .telephony import Telephony

log = logging.getLogger("nagme.calls")

TICK_SECONDS = 15
BUSY_BACKOFF = timedelta(minutes=2)  # a due call waits this long if another is live
DIAL_TIMEOUT = timedelta(minutes=3)  # dialing with no word from Twilio means a broken setup
TWILIO_FINAL = {"completed", "busy", "no-answer", "failed", "canceled"}


class CallManager:
    def __init__(
        self,
        db: Database,
        cfg: Config,
        telephony: Telephony,
        *,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.telephony = telephony
        self.clock = clock
        self.rng = rng or random.Random()

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
        self._expire_stale_calls(now)

        if s.active and s.phone_number:
            if s.next_call_at is None:
                s.next_call_at = next_call_time(now, s, self.rng)
                self.db.save_settings(s)
            elif now >= s.next_call_at:
                self.db.create_call("scheduled", now)
                s.next_call_at = next_call_time(now, s, self.rng)
                self.db.save_settings(s)
                log.info("Next scheduled call at %s", s.next_call_at)

        for call in self.db.due_calls(now):
            if call.kind != "manual" and not (s.active and in_work_hours(now, s)):
                self.db.update_call(call.id, status="skipped", error="Outside work hours or paused")
                continue
            if self.db.open_calls():
                self.db.update_call(call.id, scheduled_for=now + BUSY_BACKOFF)
                continue
            await self.place(call, s)

    def apply_settings(self, s: Settings) -> None:
        """Save settings and restart the schedule from now."""
        now = self.clock()
        s.next_call_at = next_call_time(now, s, self.rng) if s.active else None
        if not s.active:
            for call in self.db.pending_calls():
                self.db.update_call(call.id, status="skipped", error="Schedule paused")
        self.db.save_settings(s)

    # Dialing

    async def call_now(self) -> Call:
        if self.db.open_calls():
            raise RuntimeError("A call is already in progress.")
        s = self.db.get_settings()
        if not s.phone_number:
            raise RuntimeError("Add your phone number in Settings first.")
        call = self.db.create_call("manual", self.clock())
        await self.place(call, s)
        return self.db.get_call(call.id) or call

    async def place(self, call: Call, s: Settings) -> None:
        token = secrets.token_urlsafe(24)
        self.db.update_call(call.id, status="dialing", stream_token=token, dialed_at=self.clock())
        try:
            sid = await asyncio.to_thread(self.telephony.place_call, s.phone_number, call.id)
        except Exception as exc:
            log.warning("Could not place call %s: %s", call.id, exc)
            self.db.update_call(call.id, status="failed", error=str(exc)[:500])
            return
        self.db.update_call(call.id, twilio_sid=sid)
        log.info("Dialing call %s (%s)", call.id, sid)

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
            # Busy, no answer, declined, or (on a trial account) answered without
            # pressing a key: either way, you dodged it.
            self.db.update_call(call.id, status="missed", ended_at=now)
            self._schedule_retry(call, now)

    def _schedule_retry(self, call: Call, now: datetime) -> None:
        s = self.db.get_settings()
        if call.kind == "manual" or not s.active or call.attempt >= s.max_retries:
            return
        retry_at = now + timedelta(minutes=s.retry_delay_minutes)
        self.db.create_call("retry", retry_at, attempt=call.attempt + 1)
        log.info("Call %s was missed; retrying at %s", call.id, retry_at)

    def _expire_stale_calls(self, now: datetime) -> None:
        for call in self.db.open_calls():
            if call.status == "dialing" and call.dialed_at and now - call.dialed_at > DIAL_TIMEOUT:
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

    def save_outcome(
        self,
        call_id: int,
        summary: str,
        on_track: str,
        plan: str | None,
        follow_up_minutes: object = None,
    ) -> None:
        if on_track not in ("yes", "no", "unclear"):
            on_track = "unclear"
        self.db.update_call(
            call_id,
            summary=summary.strip()[:500] or None,
            on_track=on_track,
            plan=(plan or "").strip()[:500] or None,
        )
        if follow_up_minutes is not None:
            self.schedule_follow_up(follow_up_minutes)

    def schedule_follow_up(self, minutes: object) -> datetime | None:
        """Bring the next call forward to when the coach promised to check back."""
        try:
            minutes = int(minutes)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return None
        low, high = coach.FOLLOW_UP_RANGE
        s = self.db.get_settings()
        at = self.clock() + timedelta(minutes=max(low, min(high, minutes)))
        if not s.active or not in_work_hours(at, s):
            return None
        s.next_call_at = at
        self.db.save_settings(s)
        log.info("Coach booked a follow-up call at %s", at)
        return at

    def finish_stream(self, call_id: int, transcript: list[dict[str, str]], error: str | None):
        values: dict = {"status": "completed", "ended_at": self.clock(), "transcript": transcript}
        if error:
            values["error"] = error[:500]
        self.db.update_call(call_id, **values)

    def call_context(self, call: Call) -> coach.CallContext:
        s = self.db.get_settings()
        tz = ZoneInfo(s.timezone)
        now = self.clock()
        today = [
            c for c in self.db.calls_since(local_day_start(now, s.timezone)) if c.id != call.id
        ]
        answered = [c for c in today if c.answered_at]
        plans = [c.plan for c in answered if c.plan]
        return coach.CallContext(
            style=s.coach_style,
            now=now.astimezone(tz),
            first_of_day=not answered,
            plan=plans[-1] if plans else None,
            earlier=[
                coach.EarlierCall(c.answered_at.astimezone(tz), c.summary, c.on_track)
                for c in answered
                if c.summary and c.answered_at
            ][-5:],
            missed_today=sum(c.status == "missed" for c in today),
            can_follow_up=s.active,
        )
