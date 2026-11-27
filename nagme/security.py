"""Web hardening: keep the dashboard off the public tunnel, security headers, CSRF
tokens and a login lockout.

The tunnel exists only so Twilio can reach the webhooks. Everything else (the
dashboard, the scorecard, login) answers only to requests made on this machine,
unless ALLOW_REMOTE_DASHBOARD is set.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Routes the outside world (Twilio) may reach through the tunnel.
PUBLIC_PREFIXES = ("/twilio/", "/healthz")

# Any of these means the request came through a proxy or tunnel, not from this Mac.
# Cloudflare and ngrok always add at least one, and a remote client can't remove them.
PROXY_HEADERS = (
    b"cf-connecting-ip",
    b"cf-ray",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"forwarded",
    b"x-real-ip",
)

SECURITY_HEADERS = [
    (
        b"content-security-policy",
        b"default-src 'none'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        b"font-src https://fonts.gstatic.com; img-src 'self' data:; form-action 'self'; "
        b"frame-ancestors 'none'; base-uri 'none'",
    ),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


def is_remote(scope: Scope) -> bool:
    return any(name in PROXY_HEADERS for name, _ in scope.get("headers", []))


class GuardMiddleware:
    """Hides non-Twilio routes from the tunnel and adds security headers."""

    def __init__(self, app: ASGIApp, *, allow_remote_dashboard: bool = False) -> None:
        self.app = app
        self.allow_remote_dashboard = allow_remote_dashboard

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        public = path.startswith(PUBLIC_PREFIXES)
        if is_remote(scope) and not public and not self.allow_remote_dashboard:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"text/plain")] + SECURITY_HEADERS,
                }
            )
            await send({"type": "http.response.body", "body": b"Not found"})
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += SECURITY_HEADERS
                if not public:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# CSRF: one random token per session, required in every dashboard form.


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin != "null" and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Cross-site request blocked")
    form = await request.form()
    sent = str(form.get("csrf", ""))
    expected = request.session.get("csrf", "")
    if not sent or not expected or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="Form expired. Reload the page and retry.")


# Login lockout: 5 wrong passwords in 15 minutes locks that client out for 15 minutes.


class LoginLimiter:
    MAX_FAILURES = 5
    WINDOW = 15 * 60

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.failures: dict[str, list[float]] = defaultdict(list)

    def _recent(self, key: str) -> list[float]:
        cutoff = self.clock() - self.WINDOW
        self.failures[key] = [t for t in self.failures[key] if t > cutoff]
        return self.failures[key]

    def locked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.MAX_FAILURES

    def fail(self, key: str) -> None:
        self._recent(key).append(self.clock())

    def reset(self, key: str) -> None:
        self.failures.pop(key, None)


def client_key(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
