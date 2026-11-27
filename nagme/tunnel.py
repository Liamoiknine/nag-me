"""A free public URL via a Cloudflare quick tunnel (no account needed).

Twilio has to reach this server from the internet. If PUBLIC_URL isn't set and
`cloudflared` is installed, we start a quick tunnel and use its address. The address
changes every run, which is fine: on startup we point the Twilio number at it.
"""

from __future__ import annotations

import atexit
import logging
import re
import shutil
import subprocess
import threading
import time
import urllib.request

log = logging.getLogger("nagme.tunnel")
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def start_quick_tunnel(port: int, timeout: float = 30) -> str | None:
    binary = shutil.which("cloudflared")
    if binary is None:
        log.warning("cloudflared isn't installed (brew install cloudflared); no public URL.")
        return None
    process = subprocess.Popen(
        [binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    atexit.register(process.terminate)

    found: list[str] = []

    def read() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            match = URL_PATTERN.search(line)
            if match and not found:
                found.append(match.group(0))
        # keep draining so the pipe never fills

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not found and process.poll() is None:
        time.sleep(0.2)
    if not found:
        log.warning("The Cloudflare tunnel didn't start; no public URL.")
        process.terminate()
        return None
    log.info("Public URL: %s", found[0])
    return found[0]


def wait_until_reachable(url: str, timeout: float = 30) -> bool:
    """New quick-tunnel hostnames can take a few seconds to resolve."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=5) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False
