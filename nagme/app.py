"""The FastAPI app: the dashboard, Twilio's webhooks and the media-stream socket."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
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

from . import coach
from .bridge import OpenAIConnector, connect_openai, handle_media_stream
from .calls import CallManager
from .config import Config
from .db import COACH_STYLES, DAY_NAMES, Database, Settings
from .schedule import local_day_start
from .telephony import (
    Telephony,
    TwilioTelephony,
    UnconfiguredTelephony,
    hangup_twiml,
    is_valid_signature,
    stream_twiml,
)

log = logging.getLogger("nagme.app")
HERE = Path(__file__).parent
TIMEZONES = sorted(available_timezones())
COMMON_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "Europe/London",
]


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
    run_scheduler: bool = True,
) -> FastAPI:
    cfg = cfg or Config.from_env()
    db = Database(cfg.database_path)
    if telephony is None:
        has_twilio = cfg.twilio_account_sid and cfg.twilio_auth_token
        telephony = TwilioTelephony(cfg) if has_twilio else UnconfiguredTelephony()
    manager = CallManager(db, cfg, telephony)
    connect = openai_connect or connect_openai

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(manager.run_forever()) if run_scheduler else None
        yield
        if task:
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
    templates = Jinja2Templates(directory=HERE / "templates")

    # Auth

    def require_login(request: Request) -> None:
        if not request.session.get("signed_in"):
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    def flash(request: Request, text: str, kind: str = "ok") -> None:
        request.session["flash"] = {"text": text, "kind": kind}

    def back_home() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok"

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(
            request, "login.html", {"no_password": not cfg.admin_password, "error": None}
        )

    @app.post("/login")
    async def login(request: Request, password: str = Form("")):
        if cfg.admin_password and hmac.compare_digest(
            password.encode(), cfg.admin_password.encode()
        ):
            request.session["signed_in"] = True
            return back_home()
        await asyncio.sleep(1)  # slow down guessing
        return templates.TemplateResponse(
            request,
            "login.html",
            {"no_password": not cfg.admin_password, "error": "Wrong password."},
            status_code=401,
        )

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # Dashboard

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    async def dashboard(request: Request):
        s = db.get_settings()
        tz = ZoneInfo(s.timezone)
        now = manager.clock()
        today = db.calls_since(local_day_start(now, s.timezone))
        plans = [c.plan for c in today if c.plan]

        def local(dt: datetime | None) -> str:
            if dt is None:
                return ""
            dt = dt.astimezone(tz)
            day = "Today" if dt.date() == now.astimezone(tz).date() else f"{dt:%a %b} {dt.day}"
            return f"{day}, {coach.clock(dt)}"

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "s": s,
                "missing": cfg.missing(),
                "flash": request.session.pop("flash", None),
                "calls": db.recent_calls(30),
                "live_call": bool(db.open_calls()),
                "today_plan": plans[-1] if plans else None,
                "answered_today": sum(1 for c in today if c.answered_at),
                "missed_today": sum(1 for c in today if c.status == "missed"),
                "local": local,
                "day_names": DAY_NAMES,
                "styles": COACH_STYLES,
                "common_timezones": COMMON_TIMEZONES,
                "timezones": TIMEZONES,
            },
        )

    @app.post("/settings", dependencies=[Depends(require_login)])
    async def save_settings(
        request: Request,
        phone_number: str = Form(""),
        coach_style: str = Form("supportive"),
        timezone: str = Form("America/Chicago"),
        work_days: Annotated[list[str] | None, Form()] = None,
        start_time: str = Form("09:00"),
        end_time: str = Form("17:00"),
        interval_minutes: int = Form(60),
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
        if not 10 <= interval_minutes <= 240:
            errors.append("Check-ins must be 10 to 240 minutes apart.")
        if not 0 <= max_retries <= 3:
            errors.append("Retries must be 0 to 3.")
        if not 2 <= retry_delay_minutes <= 30:
            errors.append("Retry delay must be 2 to 30 minutes.")
        if errors:
            flash(request, " ".join(errors), "error")
            return back_home()

        current = db.get_settings()
        manager.apply_settings(
            Settings(
                phone_number=phone or "",
                coach_style=coach_style,
                timezone=timezone,
                work_days=days,
                start_time=start,
                end_time=end,
                interval_minutes=interval_minutes,
                max_retries=max_retries,
                retry_delay_minutes=retry_delay_minutes,
                active=current.active and bool(phone),
            )
        )
        flash(request, "Settings saved.")
        return back_home()

    @app.post("/toggle", dependencies=[Depends(require_login)])
    async def toggle(request: Request):
        s = db.get_settings()
        if not s.active and not s.phone_number:
            flash(request, "Add your phone number before turning calls on.", "error")
            return back_home()
        s.active = not s.active
        manager.apply_settings(s)
        flash(request, "Calls are on." if s.active else "Calls are paused.")
        return back_home()

    @app.post("/call-now", dependencies=[Depends(require_login)])
    async def call_now(request: Request):
        try:
            call = await manager.call_now()
        except RuntimeError as exc:
            flash(request, str(exc), "error")
            return back_home()
        if call.status == "failed":
            flash(request, f"Couldn't place the call: {call.error}", "error")
        else:
            flash(request, "Calling you now.")
        return back_home()

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

    def call_id_param(request: Request) -> int:
        raw = request.query_params.get("call_id", "")
        if not raw.isdigit():
            raise HTTPException(status_code=400, detail="call_id is required")
        return int(raw)

    @app.post("/twilio/voice")
    async def twilio_voice(request: Request):
        await twilio_form(request)
        call = db.get_call(call_id_param(request))
        if call is None or call.status != "dialing" or not call.stream_token:
            xml = hangup_twiml("This check-in is no longer active. Goodbye.")
        else:
            xml = stream_twiml(cfg.stream_url, call.id, call.stream_token)
        return Response(content=xml, media_type="application/xml")

    @app.post("/twilio/status")
    async def twilio_status(request: Request):
        params = await twilio_form(request)
        manager.handle_status(call_id_param(request), params.get("CallStatus", ""))
        return Response(status_code=204)

    @app.websocket("/twilio/stream")
    async def twilio_stream(ws: WebSocket):
        await handle_media_stream(ws, manager, connect)

    return app
