"""Run Nag Me.

uv run python -m nagme                 # server (+ a free tunnel if PUBLIC_URL is unset)
uv run python -m nagme --menubar       # same, plus a macOS menu bar icon
uv run python -m nagme install-login   # start at login (macOS), with the menu bar icon
uv run python -m nagme uninstall-login
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Config
from .tunnel import start_quick_tunnel

log = logging.getLogger("nagme")
LABEL = "com.nagme.server"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_FILE = Path.home() / "Library" / "Logs" / "nagme.log"
REPO = Path(__file__).resolve().parent.parent


def serve(menubar: bool, tunnel: bool) -> None:
    cfg = Config.from_env()
    port = int(os.environ.get("PORT", "8000"))
    if not cfg.public_url and tunnel:
        url = start_quick_tunnel(port)
        if url:
            cfg = dataclasses.replace(cfg, public_url=url)
    for name in cfg.missing():
        log.warning("%s is not set; see .env.example", name)

    app = create_app(cfg)
    server = uvicorn.Server(
        uvicorn.Config(app, host=os.environ.get("HOST", "127.0.0.1"), port=port, log_level="info")
    )
    if not menubar:
        server.run()
        return

    from .menubar import run_menubar  # macOS only

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        run_menubar(app.state.manager, f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def install_login() -> None:
    uv = shutil.which("uv")
    if sys.platform != "darwin" or uv is None:
        sys.exit("Start-at-login needs macOS and uv on your PATH.")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            uv,
            "run",
            "--directory",
            str(REPO),
            "python",
            "-m",
            "nagme",
            "--menubar",
        ],  # fmt: skip
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after Quit
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }
    PLIST.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    print(f"Nag Me now starts at login. Logs: {LOG_FILE}")


def uninstall_login() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print("Nag Me no longer starts at login.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="nagme", description="Run Nag Me.")
    parser.add_argument("command", nargs="?", choices=["install-login", "uninstall-login"])
    parser.add_argument("--menubar", action="store_true", help="show a macOS menu bar icon")
    parser.add_argument("--no-tunnel", action="store_true", help="don't start a tunnel")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
    if args.command == "install-login":
        install_login()
    elif args.command == "uninstall-login":
        uninstall_login()
    else:
        serve(menubar=args.menubar, tunnel=not args.no_tunnel)


if __name__ == "__main__":
    main()
