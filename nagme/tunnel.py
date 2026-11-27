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

log = logging.getLogger("nagme.tunnel")
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
DNS_SETTLE_SECONDS = 10  # time for the new hostname to publish before Twilio needs it


def start_quick_tunnel(port: int, timeout: float = 45) -> str | None:
    """Start the tunnel and return its URL once Cloudflare has registered it.

    We deliberately never look the new hostname up from this Mac: asking before the
    record is published makes macOS cache "no such host" for a while. Instead we wait
    for cloudflared to report a registered connection, then give DNS a moment.
    """
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
    registered = threading.Event()

    def read() -> None:
        assert process.stderr is not None
        for line in process.stderr:  # keep draining so the pipe never fills
            match = URL_PATTERN.search(line)
            if match and not found:
                found.append(match.group(0))
            if "Registered tunnel connection" in line:
                registered.set()

    threading.Thread(target=read, daemon=True).start()
    if not registered.wait(timeout) or not found:
        log.warning("The Cloudflare tunnel didn't start; no public URL.")
        process.terminate()
        return None
    time.sleep(DNS_SETTLE_SECONDS)
    log.info("Public URL: %s", found[0])
    return found[0]
