"""The Mac drift watcher: notices when you've been off task, so calls happen for a reason.

Every 20 seconds during work hours it records which app is in front, the domain of the
browser's active tab (domain only, never the full URL or page title), and whether
you're idle. If most of the last N minutes went to things on your distraction list, it
sends a Mac notification first. If you're still off task a few minutes later, it calls.
Everything stays in the local database, and samples older than 30 days are deleted.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlparse

from .calls import SAMPLE_SECONDS, CallManager
from .db import Sample
from .schedule import in_work_hours

log = logging.getLogger("nagme.activity")

IDLE_SECONDS = 120  # no keyboard or mouse for this long counts as away, not drifting
DRIFT_SHARE = 0.75  # share of the window spent on distractions that counts as drift
GRACE = timedelta(minutes=3)  # time between the warning notification and the call
COOLDOWN = timedelta(minutes=30)  # no new drift warnings this soon after a drift call
RETENTION = timedelta(days=30)

BROWSER_SCRIPTS = {
    "Google Chrome": 'tell application "Google Chrome" to return URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to return URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to return URL of active tab of '
    "front window",
    "Arc": 'tell application "Arc" to return URL of active tab of front window',
    "Safari": 'tell application "Safari" to return URL of front document',
}


@dataclass
class RawSample:
    app: str | None
    url: str | None
    idle_seconds: float


class Sampler(Protocol):
    def sample(self) -> RawSample | None: ...


def _run(args: list[str], timeout: float = 3) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class MacSampler:
    """Reads the front app and idle time (no permissions needed) and the browser's
    active tab (macOS asks once for permission to control the browser)."""

    def __init__(self) -> None:
        self._url_blocked_until: dict[str, datetime] = {}

    def sample(self) -> RawSample | None:
        front = _run(["lsappinfo", "front"])
        info = _run(["lsappinfo", "info", "-only", "name", front]) if front else None
        app = info.split("=", 1)[1].strip().strip('"') if info and "=" in info else None
        idle = _run(["ioreg", "-c", "IOHIDSystem"])
        idle_seconds = 0.0
        for line in (idle or "").splitlines():
            if "HIDIdleTime" in line:
                idle_seconds = int(line.rsplit("=", 1)[1].strip()) / 1e9
                break
        return RawSample(app, self._browser_url(app), idle_seconds)

    def _browser_url(self, app: str | None) -> str | None:
        script = BROWSER_SCRIPTS.get(app or "")
        if not script:
            return None
        now = datetime.now()
        if self._url_blocked_until.get(app or "", now) > now:
            return None
        url = _run(["osascript", "-e", script])
        if url is None:
            # Usually the one-time macOS automation permission hasn't been granted.
            if app not in self._url_blocked_until:
                log.warning(
                    "Can't read %s's active tab. Allow it under System Settings > Privacy "
                    "& Security > Automation.",
                    app,
                )
            self._url_blocked_until[app or ""] = now + timedelta(minutes=10)
        return url


def mac_notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        log.info("%s: %s", title, message)
        return
    script = [
        "osascript",
        "-e",
        "on run argv",
        "-e",
        "display notification (item 2 of argv) with title (item 1 of argv)",
        "-e",
        "end run",
        title,
        message,
    ]
    try:
        # Fire and forget, so a notification never stalls a live call's audio.
        subprocess.Popen(script, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        log.info("%s: %s", title, message)


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.") or None


def is_distracted(app: str | None, domain: str | None, rules: list[str]) -> bool:
    for rule in rules:
        if domain and (domain == rule or domain.endswith("." + rule)):
            return True
        if app and app.lower() == rule:
            return True
    return False


def _label(sample: Sample) -> str:
    return sample.domain or sample.app or "something"


class DriftWatcher:
    def __init__(
        self,
        manager: CallManager,
        sampler: Sampler,
        notify: Callable[[str, str], None],
    ) -> None:
        self.manager = manager
        self.db = manager.db
        self.sampler = sampler
        self.notify = notify
        self.warned_at: datetime | None = None
        self.quiet_until: datetime | None = None  # no new warnings before this
        self._last_prune: datetime | None = None

    async def run_forever(self) -> None:
        log.info("Drift watcher running (samples every %ss)", SAMPLE_SECONDS)
        while True:
            try:
                await self.step()
            except Exception:
                log.exception("Drift watcher step failed")
            await asyncio.sleep(SAMPLE_SECONDS)

    async def step(self) -> None:
        now = self.manager.clock()
        s = self.db.get_settings()
        if not s.drift_enabled or not in_work_hours(now, s):
            self.warned_at = None
            return
        raw = await asyncio.to_thread(self.sampler.sample)
        if raw is None:
            return
        idle = raw.idle_seconds >= IDLE_SECONDS
        domain = domain_of(raw.url)
        distracted = not idle and is_distracted(raw.app, domain, s.distraction_list)
        self.db.add_sample(Sample(now, raw.app, domain, distracted, idle))
        self.check(now)
        if self._last_prune is None or now - self._last_prune > timedelta(hours=6):
            self.db.prune_activity(now - RETENTION)
            self._last_prune = now

    def check(self, now: datetime) -> None:
        """Warn, then call, if most of the recent window went to distractions."""
        s = self.db.get_settings()
        window = timedelta(minutes=s.drift_minutes)
        samples = self.db.samples_between(now - window, now + timedelta(seconds=1))
        expected = window.total_seconds() / SAMPLE_SECONDS
        # History must span the whole window (gaps inside it are fine, e.g. after sleep).
        covers = bool(samples) and samples[0].at <= now - window + timedelta(
            seconds=2 * SAMPLE_SECONDS
        )
        drifting = covers and len(samples) >= 0.8 * expected and _share(samples) >= DRIFT_SHARE

        if not drifting:
            recent = self.db.samples_between(now - GRACE, now + timedelta(seconds=1))
            if self.warned_at and _share(recent) < 0.5:
                self.warned_at = None  # they got back on track after the warning
            return

        last_call = self.manager.last_drift_call_at()
        snoozed = s.snoozed_until is not None and now < s.snoozed_until
        cooling = (last_call and now - last_call < COOLDOWN) or (
            self.quiet_until is not None and now < self.quiet_until
        )
        if cooling or snoozed or self.db.open_calls():
            return

        reason = self._describe(samples, s.drift_minutes)
        if self.warned_at is None:
            self.warned_at = now
            calling = f" Calling in {int(GRACE.total_seconds() // 60)} min." if s.active else ""
            self.notify("Nag Me", f"{reason}{calling}")
            log.info("Drift warning sent")
            return
        recent = self.db.samples_between(now - GRACE, now + timedelta(seconds=1))
        if now - self.warned_at >= GRACE and _share(recent) >= DRIFT_SHARE:
            # Whether or not a call goes out (calls may be paused), this drift episode
            # is handled; don't warn again for a while.
            self.warned_at = None
            self.quiet_until = now + COOLDOWN
            self.manager.queue_drift_call(reason)

    def _describe(self, samples: list[Sample], minutes: int) -> str:
        counts = Counter(_label(x) for x in samples if x.distracted)
        top = ", ".join(
            f"{label} ({round(n * SAMPLE_SECONDS / 60)} min)" for label, n in counts.most_common(2)
        )
        text = f"Mostly on {top} over the last {minutes} min."
        upcoming = [c for c in self.db.open_commitments()]
        if upcoming:
            nxt = upcoming[0]
            text += f" Open commitment: {nxt.text}, due {self.manager.local_time(nxt.due_at)}."
        return text


def _share(samples: list[Sample]) -> float:
    return sum(x.distracted for x in samples) / len(samples) if samples else 0.0
