"""Publish the console through a Cloudflare quick tunnel.

`cloudflared tunnel --url http://127.0.0.1:PORT` opens an outbound connection to
Cloudflare and gets back a random `*.trycloudflare.com` hostname. No account, no
DNS, no inbound firewall change — which is exactly why it deserves care rather
than convenience: the result is a public URL in front of a process that runs
commands. `cli.serve` refuses `--tunnel` without a password for that reason.

Two properties worth knowing. The hostname is *random but not secret* — it goes
over the wire and Cloudflare knows it — so it is obscurity, not access control.
And because cloudflared connects to the console over loopback, the console's own
"refuse a non-loopback bind" check never fires; the tunnel goes around it by
design, and the password is what replaces it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: cloudflared prints the assigned hostname to stderr in a banner. Match the URL
#: itself rather than the banner, which has changed shape between releases.
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

#: How long to wait for that line before giving up. A cold start negotiates TLS
#: to Cloudflare's edge; 60s is generous rather than tight.
STARTUP_TIMEOUT_S = 60


class CloudflaredMissing(RuntimeError):
    """cloudflared could not be found, or the copy that was found cannot run."""


def find_cloudflared() -> str:
    """Locate a cloudflared that actually executes.

    `shutil.which` is not enough on Windows. The npm package installs a
    `cloudflared.CMD` shim ahead of the real binary on PATH, and that shim
    swallowed both stdout and the exit status here — the tunnel appeared to exit
    instantly having printed nothing, which is an unhelpful way to learn anything.

    So: prefer a native executable, fall back to whatever is on PATH, and let
    `TRACKER_CLOUDFLARED` override the lot for an unusual install.
    """
    override = os.environ.get("TRACKER_CLOUDFLARED")
    if override:
        return override

    candidates: list[str] = []
    if sys.platform == "win32":
        # Where `npm i -g cloudflared` puts the binary it downloads.
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(
                str(Path(appdata) / "npm/node_modules/cloudflared/bin/cloudflared.exe")
            )
        found = shutil.which("cloudflared.exe")
        if found:
            candidates.append(found)
    candidates.append(shutil.which("cloudflared") or "")

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate

    raise CloudflaredMissing(
        "cloudflared is not on PATH.\n"
        "  npm install -g cloudflared\n"
        "  winget install --id Cloudflare.cloudflared\n"
        "  brew install cloudflared\n\n"
        "Set TRACKER_CLOUDFLARED to an explicit path if it lives somewhere unusual."
    )


def _check_runnable(binary: str) -> None:
    """Fail with the real reason rather than an empty tunnel log.

    A truncated download is a PE file with a valid header and no body, so it
    passes every check short of running it. Observed here: npm's postinstall left
    a 7.9 MB `cloudflared.exe` where the real one is 54 MB, and every attempt to
    launch it died with WinError 193 and no output at all.
    """
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except OSError as exc:
        raise CloudflaredMissing(
            f"{binary} exists but will not run: {exc}\n\n"
            "A truncated download looks exactly like this. Reinstall it:\n"
            "  npm install -g cloudflared\n"
            "or, for the npm package specifically:\n"
            "  node <npm-root>/cloudflared/lib/cloudflared.js bin install latest"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CloudflaredMissing(f"{binary} did not respond to --version") from exc
    if result.returncode != 0:
        raise CloudflaredMissing(
            f"{binary} --version exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
    log.debug("cloudflared: %s", (result.stdout or "").strip())


@dataclass
class Tunnel:
    url: str
    process: subprocess.Popen

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


def quick_tunnel(port: int, *, timeout_s: int = STARTUP_TIMEOUT_S) -> Tunnel:
    """Start cloudflared against a local port and return once it has a URL."""
    binary = find_cloudflared()
    _check_runnable(binary)

    process = subprocess.Popen(
        [
            binary,
            "tunnel",
            "--no-autoupdate",
            "--url",
            f"http://127.0.0.1:{port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    found: list[str] = []
    tail: list[str] = []

    def read() -> None:
        # Keep draining after the URL is found: cloudflared blocks on a full pipe,
        # which would stall the tunnel a few hundred lines into a session.
        assert process.stdout is not None
        for line in process.stdout:
            log.debug("cloudflared: %s", line.rstrip())
            tail.append(line.rstrip())
            del tail[:-40]
            if not found:
                match = _URL.search(line)
                if match:
                    found.append(match.group(0))

    threading.Thread(target=read, daemon=True).start()

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if found:
            return Tunnel(url=found[0], process=process)
        if process.poll() is not None:
            raise TimeoutError(
                "cloudflared exited before publishing a URL:\n  " + "\n  ".join(tail[-10:])
            )
        time.sleep(0.2)

    process.terminate()
    raise TimeoutError(
        f"cloudflared did not publish a URL within {timeout_s}s:\n  " + "\n  ".join(tail[-10:])
    )


__all__ = [
    "STARTUP_TIMEOUT_S",
    "CloudflaredMissing",
    "Tunnel",
    "find_cloudflared",
    "quick_tunnel",
]
