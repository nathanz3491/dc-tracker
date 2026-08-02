"""Publish the console through Cloudflare, in either of the two shapes cloudflared offers.

**Quick tunnel.** `cloudflared tunnel --url http://127.0.0.1:PORT` opens an
outbound connection to Cloudflare and gets back a random `*.trycloudflare.com`
hostname. No account, no DNS, no inbound firewall change — which is exactly why
it deserves care rather than convenience: the result is a public URL in front of
a process that runs commands. Both `tracker serve --tunnel` and
`tracker cloudflare` refuse to start one without a password for that reason.

**Named tunnel.** `cloudflared tunnel --url http://127.0.0.1:PORT run NAME` runs
a tunnel you created once against your own Cloudflare account, so the hostname is
yours and survives a restart. Worth the setup if the link is going to be shared
with anybody, because a quick-tunnel URL changes every time and is therefore
either re-sent constantly or written down somewhere it should not be.

Creating that tunnel is deliberately **not** done here. `cloudflared tunnel
create` and `cloudflared tunnel route dns` write credentials into your home
directory and a DNS record into your zone; both outlive this process, and neither
is something a tracker command should do on your behalf. :func:`named_tunnel`
runs a tunnel that already exists and tells you the two commands if it does not.

Two properties worth knowing about either shape. A quick-tunnel hostname is
*random but not secret* — it goes over the wire and Cloudflare knows it — so it
is obscurity, not access control. And because cloudflared connects to the console
over loopback, the console's own "refuse a non-loopback bind" check never fires;
the tunnel goes around it by design, and the password is what replaces it.
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
from typing import Any

log = logging.getLogger(__name__)

#: cloudflared prints the assigned hostname to stderr in a banner. Match the URL
#: itself rather than the banner, which has changed shape between releases.
#:
#: `api.` is excluded and that exclusion is load-bearing, not tidiness.
#: `https://api.trycloudflare.com/tunnel` is the endpoint cloudflared *asks* for a
#: tunnel, and it appears in the failure message when that request times out —
#: so without this, a tunnel that never got created was reported as
#: `public: https://api.trycloudflare.com` and the operator was handed a link to
#: Cloudflare's API as though it were their console. Observed exactly that way.
_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")

#: Lines that mean the quick tunnel will never arrive. Without these the request
#: failure above is simply waited out, and the operator gets a 60-second pause
#: followed by a timeout instead of the reason on the first line of output.
_QUICK_FAILED = re.compile(r"failed to (request|serve|create) quick Tunnel", re.I)

#: A named tunnel prints no URL — it already has one. What it does print, once
#: per edge connection, is a registration line. Two spellings because the wording
#: changed between cloudflared releases and both are still in the wild.
_REGISTERED = re.compile(r"Registered tunnel connection|Connection .* registered", re.I)

#: How long to wait for that line before giving up. A cold start negotiates TLS
#: to Cloudflare's edge; 60s is generous rather than tight.
STARTUP_TIMEOUT_S = 60


class CloudflaredMissing(RuntimeError):
    """cloudflared could not be found, or the copy that was found cannot run."""


class TunnelNotFound(RuntimeError):
    """A named tunnel was asked for and your account does not have one."""


class TunnelFailed(RuntimeError):
    """cloudflared ran, reached the network, and did not come back with a tunnel."""


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


def version(binary: str | None = None) -> str:
    """The cloudflared build in use, for a preflight report."""
    binary = binary or find_cloudflared()
    _check_runnable(binary)
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    return (result.stdout or result.stderr or "").strip().splitlines()[0] if result.stdout else ""


def named_tunnels(binary: str | None = None) -> list[str]:
    """Every named tunnel this machine's credentials can run.

    Empty rather than raising when the account has none or nobody has logged in:
    "you have no named tunnels" and "cloudflared is not authenticated" both lead
    to the same next step, and the caller prints it.
    """
    binary = binary or find_cloudflared()
    try:
        result = subprocess.run(
            [binary, "tunnel", "list", "--output", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        import json

        rows = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return [str(r.get("name")) for r in rows if isinstance(r, dict) and r.get("name")]


#: Where cloudflared asks for a quick tunnel. Fixed, and the relay below only
#: ever forwards here — it appends the request path to this and nothing else, so
#: it cannot be used as a general-purpose proxy by anything that finds the port.
QUICK_API = "https://api.trycloudflare.com"


def detect_proxy() -> str | None:
    """The HTTP proxy this machine is configured to use, if any.

    Environment first, then the Windows registry. The registry half is the point:
    Windows applications read `Internet Settings` and Go programs do not, so a
    machine can be correctly configured and cloudflared still go direct.
    """
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value if "://" in value else f"http://{value}"

    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        with key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return None
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except (OSError, FileNotFoundError):
        return None
    if not server or "=" in server:
        # A per-protocol string ("http=host:1;https=host:2"). Parsing that
        # correctly is more surface than it is worth; a plain host:port covers
        # every case seen, and going direct is a safe answer for the rest.
        return None
    return server if "://" in server else f"http://{server}"


class _QuickRelay:
    """A loopback stand-in for `api.trycloudflare.com` that honours the proxy.

    **Why this exists.** cloudflared builds its own `http.Transport` for the
    quick-tunnel request, and a zero-value Transport has no `Proxy` function, so
    that one request ignores `HTTPS_PROXY` however it is set. Measured on a
    machine behind a local proxy: the request takes 13-25 seconds direct and
    about 4 through the proxy, against a fixed client budget of roughly ten — so
    `tracker cloudflare` failed every time with `context deadline exceeded` while
    every other tool on the machine worked.

    The relay is pointed at by cloudflared's `--quick-service` flag. It forwards
    the one request it will ever receive to :data:`QUICK_API` *through* the proxy
    and hands the JSON back. The outbound leg is still TLS to Cloudflare; the
    only plaintext hop is inside loopback, which is where cloudflared and this
    process already talk.

    It is not a general proxy. The upstream is fixed, only the path travels, and
    it binds an ephemeral loopback port that is closed with the tunnel.
    """

    def __init__(self, proxy: str) -> None:
        self.proxy = proxy
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> str:
        import http.server
        import socketserver
        import urllib.error
        import urllib.request

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
        )
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                log.debug("quick-relay: " + str(args))

            def do_POST(self) -> None:  # BaseHTTPRequestHandler's naming contract
                # Only an ordinary absolute path. An absolute request URI would
                # concatenate into `https://api.trycloudflare.comhttp://elsewhere`,
                # which is not reachable but is nonsense to hand to a URL parser,
                # and `//host/x` is the shape that turns string concatenation into
                # a redirect in the first place. Neither is anything cloudflared
                # sends, so refusing costs nothing.
                if not self.path.startswith("/") or self.path.startswith("//"):
                    self.send_error(400, "unexpected request target")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                request = urllib.request.Request(QUICK_API + self.path, data=body, method="POST")
                for header in ("Content-Type", "User-Agent", "Accept"):
                    if self.headers.get(header):
                        request.add_header(header, self.headers[header])
                try:
                    response = opener.open(request, timeout=45)
                    payload, status = response.read(), response.status
                except urllib.error.HTTPError as exc:
                    payload, status = exc.read(), exc.code
                except Exception as exc:
                    log.warning("quick-relay: %s", exc)
                    payload, status = b'{"error":"relay failed"}', 502
                log.debug("quick-relay: %s -> %s", self.path, status)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        relay.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.debug("quick-relay on 127.0.0.1:%d via %s", self.port, self.proxy)
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


@dataclass
class Tunnel:
    #: The public address, when cloudflared told us one or you did. A named
    #: tunnel run without `--hostname` genuinely has a URL and this process has
    #: no way to learn it — the DNS route lives in your Cloudflare zone, not in
    #: the tunnel's output — so None means "unknown", never "none exists".
    url: str | None
    process: subprocess.Popen
    kind: str = "quick"
    #: False when the startup window closed with the process still alive and no
    #: registration line seen. Reported rather than treated as failure: an
    #: unrecognised log format is far likelier than a tunnel that silently is not
    #: working, and killing a working tunnel over a regex would be the worse bug.
    confirmed: bool = True
    #: The proxy the quick-tunnel request was routed through, when one was used.
    via_proxy: str | None = None
    _relay: _QuickRelay | None = None

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self._relay is not None:
            self._relay.stop()


def _spawn(
    argv: list[str], pattern: re.Pattern[str]
) -> tuple[subprocess.Popen, list[str], list[str]]:
    """Start cloudflared and drain its output, watching for `pattern`.

    Returns the process, the list the first match lands in, and a rolling tail
    kept for the error message. Draining never stops: cloudflared blocks on a
    full pipe, which would stall the tunnel a few hundred lines into a session.
    """
    process = subprocess.Popen(
        argv,
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
        assert process.stdout is not None
        for line in process.stdout:
            log.debug("cloudflared: %s", line.rstrip())
            tail.append(line.rstrip())
            del tail[:-40]
            if not found:
                match = pattern.search(line)
                if match:
                    found.append(match.group(0))

    threading.Thread(target=read, daemon=True).start()
    return process, found, tail


def _wait(
    process: subprocess.Popen,
    found: list[str],
    tail: list[str],
    *,
    timeout_s: int,
    waiting_for: str,
    give_up_on: re.Pattern[str] | None = None,
) -> bool:
    """Block until the pattern matched, the process died, or time ran out.

    True if it matched. False if the window closed with the process still alive —
    the caller decides whether that is fatal. A dead process always raises,
    because that one is unambiguous, and so does `give_up_on` appearing in the
    output: cloudflared can report that it could not get a tunnel and then sit
    there, so waiting the full window out would replace a precise error with a
    vague one a minute later.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if found:
            return True
        if give_up_on is not None and any(give_up_on.search(line) for line in tail):
            raise TunnelFailed("cloudflared could not get a tunnel:\n  " + "\n  ".join(tail[-6:]))
        if process.poll() is not None:
            raise TimeoutError(
                f"cloudflared exited before {waiting_for}:\n  " + "\n  ".join(tail[-10:])
            )
        time.sleep(0.2)
    return False


def quick_tunnel(
    port: int,
    *,
    timeout_s: int = STARTUP_TIMEOUT_S,
    proxy: str | None = None,
    use_proxy: bool = True,
    attempts: int = 3,
) -> Tunnel:
    """Start an anonymous tunnel and return once Cloudflare has assigned a URL.

    Retries, because the failure it is retrying is a latency race rather than a
    refusal. Measured against `api.trycloudflare.com` from one filtered link over
    an hour: 3.8s to 28s direct, depending entirely on when you asked, against a
    fixed client budget of roughly ten seconds. Nothing about the request is
    wrong when it fails; it was just a slow minute.

    Args:
        proxy: force a specific proxy for the API request. Detected when omitted.
        use_proxy: set False to let cloudflared reach the API however it likes.
            Worth having, because the relay is a workaround for somebody else's
            HTTP client and the right escape hatch for a workaround is off.
        attempts: total tries, not extra ones.
    """
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return _quick_tunnel_once(port, timeout_s=timeout_s, proxy=proxy, use_proxy=use_proxy)
        except (TunnelFailed, TimeoutError) as exc:
            last = exc
            if attempt < attempts:
                log.warning("quick tunnel attempt %d/%d failed, retrying", attempt, attempts)
                time.sleep(2)
    assert last is not None
    raise last


def _quick_tunnel_once(
    port: int,
    *,
    timeout_s: int,
    proxy: str | None,
    use_proxy: bool,
) -> Tunnel:
    binary = find_cloudflared()
    _check_runnable(binary)

    argv = [binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]

    relay: _QuickRelay | None = None
    chosen = proxy or (detect_proxy() if use_proxy else None)
    if use_proxy and chosen:
        relay = _QuickRelay(chosen)
        try:
            argv += ["--quick-service", relay.start()]
        except OSError as exc:
            # A relay we cannot bind is not a reason to refuse to try at all.
            log.warning("could not start the proxy relay, going direct: %s", exc)
            relay = None

    def cleanup(process: subprocess.Popen | None = None) -> None:
        if process is not None:
            process.terminate()
        if relay is not None:
            relay.stop()

    process, found, tail = _spawn(argv, _URL)
    try:
        matched = _wait(
            process,
            found,
            tail,
            timeout_s=timeout_s,
            waiting_for="publishing a URL",
            give_up_on=_QUICK_FAILED,
        )
    except BaseException:
        cleanup(process)
        raise
    if matched:
        return Tunnel(
            url=found[0],
            process=process,
            kind="quick",
            via_proxy=chosen if relay is not None else None,
            _relay=relay,
        )

    # No URL and still running is fatal here, unlike the named case: a quick
    # tunnel's whole output is that URL, so not having seen it means we have
    # nothing to hand the operator.
    cleanup(process)
    raise TimeoutError(
        f"cloudflared did not publish a URL within {timeout_s}s:\n  " + "\n  ".join(tail[-10:])
    )


def named_tunnel(
    port: int,
    name: str,
    *,
    hostname: str | None = None,
    timeout_s: int = STARTUP_TIMEOUT_S,
) -> Tunnel:
    """Run an existing named tunnel in front of the console.

    `--url` before `run` supplies the ingress rule, so no `config.yml` is needed
    and the port cannot drift out of agreement with the one the console is
    actually listening on.

    Raises :class:`TunnelNotFound` if the account has no tunnel by that name,
    rather than letting cloudflared fail with its own message thirty seconds
    later. Creating one is not attempted — see the module docstring.
    """
    binary = find_cloudflared()
    _check_runnable(binary)

    existing = named_tunnels(binary)
    if existing and name not in existing:
        raise TunnelNotFound(
            f"no Cloudflare tunnel named {name!r}. This account has: {', '.join(sorted(existing))}"
        )
    if not existing:
        raise TunnelNotFound(
            f"cloudflared lists no named tunnels, so {name!r} cannot be run.\n\n"
            "Creating one writes credentials into your home directory and a DNS\n"
            "record into your zone, so it is left to you rather than done here:\n"
            f"  cloudflared tunnel login\n"
            f"  cloudflared tunnel create {name}\n"
            f"  cloudflared tunnel route dns {name} console.example.com\n\n"
            "Then re-run with --name and --hostname. Or drop both flags for an\n"
            "anonymous quick tunnel, which needs no account at all."
        )

    argv = [binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}", "run", name]
    process, found, tail = _spawn(argv, _REGISTERED)
    try:
        confirmed = _wait(
            process, found, tail, timeout_s=timeout_s, waiting_for="registering a connection"
        )
    except BaseException:
        process.terminate()
        raise
    url = f"https://{hostname}" if hostname else None
    return Tunnel(url=url, process=process, kind="named", confirmed=confirmed)


__all__ = [
    "QUICK_API",
    "STARTUP_TIMEOUT_S",
    "CloudflaredMissing",
    "Tunnel",
    "TunnelFailed",
    "TunnelNotFound",
    "detect_proxy",
    "find_cloudflared",
    "named_tunnel",
    "named_tunnels",
    "quick_tunnel",
    "version",
]
