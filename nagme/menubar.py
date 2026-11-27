"""A macOS menu bar icon: what's next, plus quick controls. Runs in the same process as
the server (`uv run python -m nagme --menubar`)."""

from __future__ import annotations

import webbrowser

import rumps

from .calls import CallManager
from .schedule import local_date


class NagMeMenu(rumps.App):
    def __init__(self, manager: CallManager, dashboard_url: str) -> None:
        super().__init__("Nag Me", title="NM", quit_button="Quit Nag Me")
        self.manager = manager
        self.dashboard_url = dashboard_url
        self.status_item = rumps.MenuItem("…")
        self.toggle_item = rumps.MenuItem("Pause calls", callback=self.toggle)
        self.menu = [
            self.status_item,
            None,
            rumps.MenuItem("Start a focus session…", callback=self.start_session),
            rumps.MenuItem("Snooze 1 hour", callback=self.snooze),
            self.toggle_item,
            None,
            rumps.MenuItem("Open dashboard", callback=self.open_dashboard),
        ]
        self.refresh(None)
        rumps.Timer(self.refresh, 30).start()

    def refresh(self, _sender) -> None:
        m = self.manager
        s = m.db.get_settings()
        now = m.clock()
        open_items = m.db.open_commitments()
        if m.db.open_calls():
            title, status = "NM ☎", "On a call"
        elif not s.active:
            title, status = "NM ⏸", "Calls paused"
        elif s.snoozed_until and s.snoozed_until > now:
            title, status = "NM 💤", f"Snoozed until {m.local_time(s.snoozed_until)}"
        elif open_items:
            nxt = open_items[0]
            overdue = nxt.due_at < now
            title = "NM !" if overdue else f"NM {m.local_time(nxt.due_at)}"
            status = f"{'Overdue' if overdue else 'Next'}: {nxt.text} ({m.local_time(nxt.due_at)})"
        else:
            plan = m.db.get_plan(local_date(now, s))
            title, status = "NM ✓", f"Plan: {plan}" if plan else "Nothing due"
        self.title = title
        self.status_item.title = status[:60]
        self.toggle_item.title = "Pause calls" if s.active else "Turn calls on"

    def start_session(self, _sender) -> None:
        window = rumps.Window(
            message="What are you working on, and for how many minutes?",
            title="Start a focus session",
            default_text="Draft the memo, 45",
            ok="Start",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        response = window.run()
        if not response.clicked or not response.text.strip():
            return
        what, _, minutes = response.text.rpartition(",")
        if not what or not minutes.strip().isdigit():
            what, minutes = response.text, "45"
        c = self.manager.add_commitment(what.strip(), int(minutes), source="menubar")
        rumps.notification("Nag Me", "Session started", f"{c.text}: check at "
                           f"{self.manager.local_time(c.due_at)}")  # fmt: skip
        self.refresh(None)

    def snooze(self, _sender) -> None:
        self.manager.snooze(60)
        self.refresh(None)

    def toggle(self, _sender) -> None:
        s = self.manager.db.get_settings()
        if not s.phone_number:
            rumps.alert("Add your phone number in the dashboard first.")
            return
        s.active = not s.active
        self.manager.apply_settings(s)
        self.refresh(None)

    def open_dashboard(self, _sender) -> None:
        webbrowser.open(self.dashboard_url)


def run_menubar(manager: CallManager, dashboard_url: str) -> None:
    """Blocks on the main thread; run the server on a background thread first."""
    NagMeMenu(manager, dashboard_url).run()
