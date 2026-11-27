"""The FastAPI app: dashboard, scorecard, Twilio webhooks and the media-stream socket."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, time
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, available_timezones

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import coach, scorecard
from .activity import DriftWatcher, MacSampler, Sampler, mac_notify
from .bridge import OpenAIConnector, connect_openai, handle_media_stream
from .calls import CallManager, Notifier
from .config import Config
from .db import COACH_STYLES, DAY_NAMES, Database
from .schedule import day_bounds, local_date
from .telephony import (
    Telephony,
    TwilioTelephony,
    UnconfiguredTelephony,
    hangup_twiml,
    is_valid_signature,
    message_twiml,
    stream_twiml,
)
from .texts import Parser, handle_text, openai_parser

log = logging.getLogger("nagme.app")
HERE = Path(__file__).parent
TIMEZONES = sorted(available_timezones())
COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "Europe/London",
]  # fmt: skip
PURPOSE_LABELS = {
    "morning": "morning plan",
    "deadline": "deadline check",
    "drift": "drift",
    "review": "day review",
    "manual": "on demand",
    "inbound": "you called",
}


def normalize_phone(raw: str) -> str | None:
    """Turn '(314) 555-0100' or '+1 314 555 0100' into E.164, assuming US numbers."""
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+"):
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def create_app(
    cfg: Config | None = None,
    *,
    telephony: Telephony | None = None,
    openai_connect: OpenAIConnector | None = None,
    text_parser: Parser | None = None,
    sampler: Sampler | None = None,
    notify: Notifier | None = None,
    run_background: bool = True,
) -> FastAPI:
    cfg = cfg or Config.from_env()
    db = Database(cfg.database_path)
    if telephony is None:
        has_twilio = cfg.twilio_account_sid and cfg.twilio_auth_token
        telephony = TwilioTelephony(cfg) if has_twilio else UnconfiguredTelephony()
    notify = notify or mac_notify
    manager = CallManager(db, cfg, telephony, notify=notify)
    if sampler is None and sys.platform == "darwin":
        sampler = MacSampler()
    watcher = DriftWatcher(manager, sampler, notify) if sampler else None
    connect = openai_connect or connect_openai
    parse = text_parser or openai_parser(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if run_background:
            tasks.append(asyncio.create_task(manager.run_forever()))
            if watcher:
                tasks.append(asyncio.create_task(watcher.run_forever()))
            if cfg.public_url and not cfg.missing():
                try:
                    log.info(await asyncio.to_thread(telephony.configure_number, cfg.public_url))
                except Exception as exc:
                    log.warning("Couldn't point the Twilio number at this server: %s", exc)
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Nag Me", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.secret_key,
        same_site="lax",
        # Not secure-only: the dashboard is also used over plain http://localhost.
        https_only=False,
        max_age=30 * 24 * 3600,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.manager = manager
    app.state.watcher = watcher
    templates = Jinja2Templates(directory=HERE / "templates")

    # Helpers

    def require_login(request: Request) -> None:
        if not request.session.get("signed_in"):
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    def flash(request: Request, text: str, kind: str = "ok") -> None:
        request.session["flash"] = {"text": text, "kind": kind}

    def back(to: str = "/") -> RedirectResponse:
        return RedirectResponse(to, status_code=303)

    def local_fmt(dt: datetime | None) -> str:
        if dt is None:
            return ""
        s = db.get_settings()
        tz = ZoneInfo(s.timezone)
        now = manager.clock().astimezone(tz)
        dt = dt.astimezone(tz)
        if dt.date() == now.date():
            return coach.clock(dt)
        return f"{dt:%a %b} {dt.day}, {coach.clock(dt)}"

    def render(request: Request, name: str, **context):
        context.setdefault("flash", request.session.pop("flash", None))
        return templates.TemplateResponse(
            request, name, {"local": local_fmt, "labels": PURPOSE_LABELS, **context}
        )

    # Auth

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return render(request, "login.html", no_password=not cfg.admin_password, error=None)

    @app.post("/login")
    async def login(request: Request, password: str = Form("")):
        if cfg.admin_password and hmac.compare_digest(
            password.encode(), cfg.admin_password.encode()
        ):
            request.session["signed_in"] = True
            return back()
        await asyncio.sleep(1)  # slow down guessing
        response = render(
            request, "login.html", no_password=not cfg.admin_password, error="Wrong password."
        )
        response.status_code = 401
        return response

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # Dashboard

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def dashboard(request: Request):
        s = db.get_settings()
        now = manager.clock()
        today = local_date(now, s)
        commitments = manager.todays_commitments()
        open_items = [c for c in commitments if c.status == "open"]
        snoozed = s.snoozed_until if s.snoozed_until and s.snoozed_until > now else None
        return render(
            request,
            "dashboard.html",
            s=s,
            now=now,
            missing=cfg.missing(),
            plan=db.get_plan(today),
            commitments=commitments,
            next_due=open_items[0] if open_items else None,
            snoozed=snoozed,
            live_call=bool(db.open_calls()),
            score=scorecard.score_day(manager, today, now),
            calls=db.recent_calls(15),
            watching=bool(watcher) and s.drift_enabled,
            day_names=DAY_NAMES,
            styles=COACH_STYLES,
            common_timezones=COMMON_TIMEZONES,
            timezones=TIMEZONES,
        )

    @app.get("/scorecard", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def scorecard_page(request: Request):
        days = scorecard.week(manager, 7)
        s = db.get_settings()
        totals = {
            "committed": sum(d.committed for d in days),
            "done": sum(d.done for d in days),
            "missed": sum(d.missed for d in days),
            "drift": sum(d.drift_minutes for d in days),
            "dodged": sum(d.dodged for d in days),
        }
        settled = totals["done"] + totals["missed"]
        totals["hit_rate"] = round(100 * totals["done"] / settled) if settled else None
        start, end = day_bounds(local_date(manager.clock(), s), s)
        return render(
            request,
            "scorecard.html",
            days=days,
            totals=totals,
            today_commitments=db.commitments_between(start, end),
        )

    @app.post("/plan", dependencies=[Depends(require_login)])
    async def save_plan(request: Request, plan: str = Form("")):
        s = db.get_settings()
        if plan.strip():
            db.set_plan(local_date(manager.clock(), s), plan.strip()[:500])
            flash(request, "Plan saved.")
        return back()

    @app.post("/commitments", dependencies=[Depends(require_login)])
    async def add_commitment(request: Request, what: str = Form(""), minutes: str = Form("")):
        try:
            c = manager.add_commitment(
                what, minutes if minutes.isdigit() else None,
                None if minutes.isdigit() else minutes, source="dashboard",
            )  # fmt: skip
        except ValueError as exc:
            flash(request, f"Couldn't add that: {exc}", "error")
            return back()
        flash(request, f"Added: {c.text}, due {manager.local_time(c.due_at)}.")
        return back()

    @app.post("/commitments/{commitment_id}", dependencies=[Depends(require_login)])
    async def resolve_commitment(request: Request, commitment_id: int, status: str = Form("")):
        try:
            manager.update_commitment(commitment_id, status)
        except ValueError as exc:
            flash(request, str(exc), "error")
        return back()

    @app.post("/snooze", dependencies=[Depends(require_login)])
    async def snooze(request: Request, minutes: int = Form(30)):
        if minutes <= 0:
            s = db.get_settings()
            s.snoozed_until = None
            db.save_settings(s)
            flash(request, "Snooze cleared.")
        else:
            until = manager.snooze(minutes)
            flash(request, f"No automatic calls until {manager.local_time(until)}.")
        return back()

    @app.post("/settings", dependencies=[Depends(require_login)])
    async def save_settings(
        request: Request,
        phone_number: str = Form(""),
        coach_style: str = Form("supportive"),
        timezone: str = Form("America/Chicago"),
        work_days: Annotated[list[str] | None, Form()] = None,
        start_time: str = Form("09:00"),
        end_time: str = Form("17:00"),
        morning_call: str = Form(""),
        review_call: str = Form(""),
        drift_enabled: str = Form(""),
        drift_minutes: int = Form(15),
        distractions: str = Form(""),
        max_retries: int = Form(2),
        retry_delay_minutes: int = Form(5),
    ):
        errors = []
        phone = normalize_phone(phone_number) if phone_number.strip() else ""
        if phone is None:
            errors.append("That phone number doesn't look right.")
        if coach_style not in COACH_STYLES:
            errors.append("Pick a coach style.")
        if timezone not in available_timezones():
            errors.append(f"Unknown time zone {timezone!r}.")
        days = tuple(DAY_NAMES.index(d) for d in work_days or [] if d in DAY_NAMES)
        if not days:
            errors.append("Pick at least one work day.")
        try:
            start, end = time.fromisoformat(start_time), time.fromisoformat(end_time)
            if end <= start:
                errors.append("Work hours must end after they start.")
        except ValueError:
            start, end = time(9), time(17)
            errors.append("Work hours must be times like 09:00.")
        if not 5 <= drift_minutes <= 60:
            errors.append("Drift time must be 5 to 60 minutes.")
        if not 0 <= max_retries <= 3:
            errors.append("Retries must be 0 to 3.")
        if not 2 <= retry_delay_minutes <= 30:
            errors.append("Retry delay must be 2 to 30 minutes.")
        if errors:
            flash(request, " ".join(errors), "error")
            return back()

        s = db.get_settings()
        s.phone_number = phone or ""
        s.coach_style = coach_style
        s.timezone = timezone
        s.work_days = days
        s.start_time, s.end_time = start, end
        s.morning_call, s.review_call = bool(morning_call), bool(review_call)
        s.drift_enabled, s.drift_minutes = bool(drift_enabled), drift_minutes
        s.distractions = "\n".join(
            line.strip().lower() for line in distractions.splitlines() if line.strip()
        )
        s.max_retries, s.retry_delay_minutes = max_retries, retry_delay_minutes
        s.active = s.active and bool(phone)
        manager.apply_settings(s)
        flash(request, "Settings saved.")
        return back()

    @app.post("/toggle", dependencies=[Depends(require_login)])
    async def toggle(request: Request):
        s = db.get_settings()
        if not s.active and not s.phone_number:
            flash(request, "Add your phone number before turning calls on.", "error")
            return back()
        s.active = not s.active
        manager.apply_settings(s)
        flash(request, "Calls are on." if s.active else "Calls are paused.")
        return back()

    @app.post("/call-now", dependencies=[Depends(require_login)])
    async def call_now(request: Request):
        try:
            call = await manager.call_now()
        except RuntimeError as exc:
            flash(request, str(exc), "error")
            return back()
        if call.status == "failed":
            flash(request, f"Couldn't place the call: {call.error}", "error")
        else:
            flash(request, "Calling you now.")
        return back()

    # Twilio webhooks

    async def twilio_form(request: Request) -> dict[str, str]:
        form = await request.form()
        params = {k: str(v) for k, v in form.multi_items()}
        url = f"{cfg.public_url}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"
        signature = request.headers.get("X-Twilio-Signature", "")
        if not is_valid_signature(cfg.twilio_auth_token, url, params, signature):
            log.warning("Rejected a webhook with a bad Twilio signature: %s", request.url.path)
            raise HTTPException(status_code=403, detail="Bad signature")
        return params

    def from_owner(params: dict[str, str]) -> bool:
        owner = db.get_settings().phone_number
        return bool(owner) and normalize_phone(params.get("From", "")) == owner

    def call_id_param(request: Request) -> int:
        raw = request.query_params.get("call_id", "")
        if not raw.isdigit():
            raise HTTPException(status_code=400, detail="call_id is required")
        return int(raw)

    def xml(body: str) -> Response:
        return Response(content=body, media_type="application/xml")

    @app.post("/twilio/voice")
    async def twilio_voice(request: Request):
        await twilio_form(request)
        call = db.get_call(call_id_param(request))
        if call is None or call.status != "dialing" or not call.stream_token:
            return xml(hangup_twiml("This check-in is no longer active. Goodbye."))
        return xml(stream_twiml(cfg.stream_url, call.id, call.stream_token))

    @app.post("/twilio/inbound")
    async def twilio_inbound(request: Request):
        params = await twilio_form(request)
        if not from_owner(params):
            return xml(hangup_twiml("Sorry, this number only takes calls from its owner."))
        if db.open_calls():
            return xml(hangup_twiml("You're already on a call with me. Goodbye."))
        call = manager.start_inbound(params.get("CallSid", ""))
        assert call.stream_token
        return xml(stream_twiml(cfg.stream_url, call.id, call.stream_token))

    @app.post("/twilio/sms")
    async def twilio_sms(request: Request):
        params = await twilio_form(request)
        if not from_owner(params):
            return xml(message_twiml(None))
        reply = await handle_text(manager, parse, params.get("Body", ""))
        notify("Nag Me", reply)
        return xml(message_twiml(reply))

    @app.post("/twilio/status")
    async def twilio_status(request: Request):
        params = await twilio_form(request)
        manager.handle_status(call_id_param(request), params.get("CallStatus", ""))
        return Response(status_code=204)

    @app.websocket("/twilio/stream")
    async def twilio_stream(ws: WebSocket):
        await handle_media_stream(ws, manager, connect)

    return app
