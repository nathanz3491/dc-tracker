"""The HTTP surface: static files, a read-only data API, and the run endpoints.

Routing is written out rather than derived, because the read/write split is the
security posture and it should be readable in one screen. Everything under
``/api/`` except ``POST /api/run`` and ``POST /api/queue/drop`` opens the database
``mode=ro``, so a bug in a read path raises instead of writing.
"""

from __future__ import annotations

import gzip
import json
import logging
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tracker import __version__
from tracker.db import MigrationError, open_db, schema_version, session_scope
from tracker.webui import assets, catalog, runs
from tracker.webui.auth import COOKIE, Gate, cookie_value
from tracker.webui.runner import Busy, Runner

log = logging.getLogger(__name__)

#: Loopback only unless the operator overrides it deliberately. This process
#: executes commands, so the bind address is a security boundary rather than a
#: convenience setting.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Below this, compressing costs more than it saves.
GZIP_MIN = 8192


class Console:
    """Shared state one server instance hands to every request."""

    def __init__(
        self, db_path: Path, *, allow_write: bool = True, password: str | None = None
    ) -> None:
        self.db_path = db_path
        self.allow_write = allow_write
        self.gate = Gate(password=password)
        self.runner = Runner(db_path)
        self._schema_version: int | None = None

    def read_session(self):
        engine = open_db(self.db_path)
        if self._schema_version is None:
            self._schema_version = schema_version(engine)
        return session_scope(engine, commit=False)

    @property
    def schema_version(self) -> int:
        return self._schema_version or 0


class Handler(BaseHTTPRequestHandler):
    console: Console  # set by serve()
    server_version = f"dc-tracker/{__version__}"
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache: str = "no-store",
        extra: dict[str, str] | None = None,
    ) -> None:
        if len(body) >= GZIP_MIN and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 6)
            encoding = "gzip"
        else:
            encoding = None
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        # The page loads only same-origin vendored files; say so, so a stray CDN
        # URL creeping into the front end fails loudly in the console instead of
        # quietly reintroducing a network dependency.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(
        self, payload: Any, status: int = 200, *, extra: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra=extra)

    def _error(self, status: int, message: str) -> None:
        """Send an error, and never become one.

        This is called from inside `except` blocks. If the peer has already gone —
        which is exactly the situation that lands here most often — writing the
        response raises a second time, out of an exception handler, and escapes to
        socketserver as an unhandled error. That is the second traceback in the
        report that started this: one for the aborted stream, one for the failed
        attempt to apologise for it.
        """
        try:
            self._json({"error": message}, status=status)
        except ConnectionError:
            log.debug("could not send %d to a client that had already gone", status)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("request body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    # --- the gate ---------------------------------------------------------

    @property
    def _session(self) -> str | None:
        return cookie_value(self.headers.get("Cookie"))

    @property
    def _authed(self) -> bool:
        return self.console.gate.valid(self._session)

    def _client(self) -> str:
        """Who is knocking, for the lockout counter.

        Behind a tunnel every request arrives from 127.0.0.1, so the socket
        address would put the whole internet in one bucket — which is not wrong
        so much as blunt. `CF-Connecting-IP` is trusted *only* when the socket is
        loopback, i.e. when the request really did come through the local
        cloudflared process; a header on a direct connection is ignored.
        """
        peer = self.client_address[0]
        if peer in {"127.0.0.1", "::1"}:
            forwarded = (
                self.headers.get("CF-Connecting-IP")
                or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            )
            if forwarded:
                return forwarded
        return peer

    def _https(self) -> bool:
        """Whether the browser reached us over TLS, for the Secure cookie flag."""
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _set_session_cookie(self, token: str | None) -> dict[str, str]:
        if token is None:
            value = f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
        else:
            # SameSite=Lax is doing real work: it is what stops another site
            # POSTing to /api/run with this cookie attached. HttpOnly keeps it
            # out of reach of any script that gets injected into the page.
            value = f"{COOKIE}={token}; Path=/; Max-Age={self.console.gate.session_ttl}; HttpOnly; SameSite=Lax"
        if self._https():
            value += "; Secure"
        return {"Set-Cookie": value}

    def _same_origin(self) -> bool:
        """Reject a cross-site state-changing request outright.

        SameSite=Lax already blocks the cookie on a cross-site POST, so this is
        the second lock rather than the first. Requests with no Origin at all
        (curl, the tests) are allowed: the cookie is the credential, and refusing
        them would break scripting the console without adding safety.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlsplit(origin).netloc == host

    # --- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's naming contract
        parsed = urlsplit(self.path)
        route, query = parsed.path, parse_qs(parsed.query)
        try:
            if not self._authed:
                return self._unauthenticated(route)
            self._route_get(route, query)
        except MigrationError as exc:
            self._error(503, str(exc))
        except FileNotFoundError as exc:
            self._error(503, str(exc))
        except ConnectionError:
            # The tab closed mid-response. Must stay ahead of the catch-all below:
            # that one tries to send a 500, and sending anything down a socket the
            # peer has already dropped raises again — which is what escaped to
            # socketserver and printed a second traceback under "Exception
            # occurred during processing of request".
            log.debug("GET %s: client went away", route)
        except Exception:
            log.exception("GET %s failed", route)
            self._error(500, "internal error; see the server log")

    do_HEAD = do_GET

    def _unauthenticated(self, route: str) -> None:
        """Serve the login form, and otherwise nothing at all.

        Deliberately blanket: no static assets, no health check, no dataset. The
        only thing an anonymous request can obtain is `login.html`, which is
        self-contained and describes nothing about the data behind it.

        `/api/` and `/static/` both get a 401 rather than the form. Returning the
        login HTML with a 200 to a request for `app.js` withholds the asset, which
        is the security part, but it also hands a browser a page where it asked
        for a script — so a session that expires mid-visit fails as a parse error
        instead of as "you are signed out".
        """
        if route.startswith(("/api/", "/static/")):
            return self._error(401, "sign in first")
        page = assets.STATIC_ROOT / "login.html"
        if not page.is_file():
            return self._error(500, "the console's login page is missing from this install")
        self._send(200, page.read_bytes(), "text/html; charset=utf-8")

    def _route_get(self, route: str, query: dict[str, list[str]]) -> None:
        if route in {"/", "/index.html"}:
            return self._page()
        if route.startswith("/static/"):
            return self._static(route[len("/static/") :])
        if route == "/api/dataset":
            return self._dataset()
        if route == "/api/commands":
            return self._json(
                {
                    "groups": catalog.grouped_json(),
                    "llm": sorted(catalog.LLM_COMMANDS),
                    "destructive": dict(catalog.DESTRUCTIVE),
                }
            )
        if route == "/api/runs":
            return self._json(
                {
                    "runs": runs.history(self.console.db_path),
                    "current": self.console.runner.snapshot(),
                }
            )
        if route.startswith("/api/run/"):
            rest = route[len("/api/run/") :]
            if rest.endswith("/stream"):
                return self._stream(rest[: -len("/stream")])
            record = runs.read_log(self.console.db_path, rest)
            return self._json(record) if record else self._error(404, f"no run {rest!r}")
        if route == "/api/health":
            return self._json({"ok": True, "version": __version__})
        self._error(404, f"no route {route!r}")

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            body = self._body()
        except ValueError as exc:
            return self._error(400, str(exc))
        try:
            if not self._same_origin():
                return self._error(403, "cross-site request refused")
            if parsed.path == "/api/login":
                return self._login(body)
            if not self._authed:
                return self._error(401, "sign in first")
            if parsed.path == "/api/logout":
                self.console.gate.revoke(self._session)
                return self._json({"ok": True}, extra=self._set_session_cookie(None))
            if parsed.path == "/api/run":
                return self._start_run(body)
            if parsed.path == "/api/run/cancel":
                return self._json({"cancelled": self.console.runner.cancel()})
            if parsed.path == "/api/overview":
                return self._overview(body)
            self._error(404, f"no route {parsed.path!r}")
        except ConnectionError:
            log.debug("POST %s: client went away", parsed.path)
        except Exception:
            log.exception("POST %s failed", parsed.path)
            self._error(500, "internal error; see the server log")

    # --- handlers ---------------------------------------------------------

    def _login(self, body: dict[str, Any]) -> None:
        gate = self.console.gate
        if not gate.required:
            return self._json({"ok": True, "note": "no password is configured"})

        client = self._client()
        remaining = gate.locked_for(client)
        if remaining:
            # Say it is a lockout and for how long. Answering "wrong password"
            # here would have an operator who mistyped twice sit there retyping a
            # correct password and never getting in.
            minutes = max(1, round(remaining / 60))
            return self._json(
                {"error": f"Too many attempts. Locked for about {minutes} more minute(s)."},
                status=429,
            )

        token = gate.attempt(str(body.get("password") or ""), client=client)
        if token is None:
            log.warning("console: failed sign-in from %s", client)
            return self._json({"error": "Wrong password."}, status=401)

        log.info("console: signed in from %s", client)
        self._json({"ok": True}, extra=self._set_session_cookie(token))

    def _page(self) -> None:
        index = assets.STATIC_ROOT / "index.html"
        if not index.is_file():
            return self._error(
                500,
                f"the console's static files are missing from {assets.STATIC_ROOT}. "
                "This is an incomplete install, not a configuration problem.",
            )
        self._send(200, index.read_bytes(), "text/html; charset=utf-8")

    def _static(self, relative: str) -> None:
        path = assets.resolve(relative)
        if path is None:
            return self._error(404, f"no asset {relative!r}")
        # `no-cache` means revalidate, not "do not store", so a repeat load is
        # still cheap. Deliberately not `immutable` even for the vendored files:
        # they are versioned by their content rather than by their URL, so an
        # upgrade would leave a browser serving last month's bundle from disk
        # with no way to ask for the new one. On loopback the saving was never
        # worth that failure mode.
        self._send(200, path.read_bytes(), assets.content_type(path), cache="no-cache")

    def _dataset(self) -> None:
        from tracker.webui.dataset import build

        with self.console.read_session() as session:
            payload = build(
                session,
                db_path=str(self.console.db_path),
                schema_version=self.console.schema_version,
            )
        payload["allow_write"] = self.console.allow_write
        payload["password_protected"] = self.console.gate.required
        self._json(payload)

    def _start_run(self, body: dict[str, Any]) -> None:
        if not self.console.allow_write:
            return self._error(403, "this console was started read-only (--no-run)")
        cmd = str(body.get("cmd") or "").strip()
        flags = body.get("flags") or {}
        if not isinstance(flags, dict):
            return self._error(400, "`flags` must be an object")
        try:
            run = self.console.runner.start(cmd, flags, confirm=body.get("confirm"))
        except catalog.InvalidRequest as exc:
            return self._error(400, str(exc))
        except Busy as exc:
            return self._error(409, str(exc))
        self._json({"run": run.summary()}, status=202)

    def _overview(self, body: dict[str, Any]) -> None:
        """Write, or return, the briefing for one project.

        A POST rather than a GET because it can spend money, and a GET that costs
        money is a GET a browser will happily make twice on a back button.

        Reads the database `mode=ro` like every other read path — the briefing is
        never stored, so there is nothing to write. Served from cache whenever the
        row has not changed since it was written; the fingerprint covers the
        sources too, because gaining a citation changes how trustworthy a row is
        without moving any value.
        """
        from tracker import overview as overview_mod
        from tracker.models import Project

        try:
            project_id = int(body.get("project_id"))
        except (TypeError, ValueError):
            return self._error(400, "project_id must be an integer")

        with self.console.read_session() as session:
            project = session.get(Project, project_id)
            if project is None:
                return self._error(404, f"no project #{project_id}")

            ready = overview_mod.cached(project)
            if ready is not None:
                return self._json({**ready.as_json(), "cached": True})

            if not self.console.allow_write:
                return self._error(403, "this console was started read-only (--no-run)")
            if str(body.get("confirm") or "").strip() != "overview":
                return self._error(
                    400, 'Writing a briefing spends LLM tokens. Re-send with confirm="overview".'
                )

            from tracker.config import get_settings
            from tracker.llm import MissingApiKey, reasoning_extractor

            try:
                extractor = reasoning_extractor(get_settings())
            except MissingApiKey as exc:
                return self._error(503, str(exc))

            written = overview_mod.write(project, extractor=extractor)

        if written is None:
            return self._error(502, "the model did not return a usable briefing")
        self._json({**written.as_json(), "cached": False})

    def _stream(self, run_id: str) -> None:
        """Server-sent events for the run in flight.

        Replays what the run has already printed before attaching, so a tab opened
        mid-run is not staring at a blank pane while output scrolls past.
        """
        runner = self.console.runner
        current = runner.current
        if current is None or current.id != run_id:
            record = runs.read_log(self.console.db_path, run_id)
            if record is None:
                return self._error(404, f"no run {run_id!r}")
            body = "".join(
                _sse({"type": "line", "line": line}) for line in record.get("lines", [])
            ) + _sse({"type": "end", "run": {k: v for k, v in record.items() if k != "lines"}})
            return self._send(200, body.encode("utf-8"), "text/event-stream; charset=utf-8")

        listener = runner.subscribe()
        backlog = list(current.lines)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for line in backlog:
                self.wfile.write(_sse({"type": "line", "line": line}).encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    event = listener.get(timeout=15)
                except queue.Empty:
                    # A comment frame keeps the connection from being reaped by an
                    # idle timeout during a long quiet phase of a crawl.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(_sse(event).encode("utf-8"))
                self.wfile.flush()
                if event.get("type") == "end":
                    break
        except ConnectionError:
            # The reader went away mid-stream. Over a Cloudflare tunnel this is
            # ordinary rather than exceptional: the edge drops an idle connection
            # and the browser's EventSource silently reconnects, so a long crawl
            # produces several of these per run.
            #
            # `ConnectionError`, not the two subclasses that used to be listed
            # here. Windows raises `ConnectionAbortedError` (WinError 10053) where
            # POSIX raises `BrokenPipeError` or `ConnectionResetError`, so the
            # narrow tuple caught nothing on the platform this runs on and every
            # closed tab logged a traceback.
            log.debug("stream for %s closed by the client", run_id)
        finally:
            runner.unsubscribe(listener)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def serve(
    db_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    allow_write: bool = True,
    password: str | None = None,
) -> None:
    """Run the console until interrupted."""
    console = Console(db_path, allow_write=allow_write, password=password)
    handler = type("BoundHandler", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    url = f"http://{host}:{port}/"
    log.info("console on %s", url)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "Console", "Handler", "serve"]
