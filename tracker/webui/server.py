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
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from tracker import __version__
from tracker.config import install_root
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

#: The views that have their own URL, one per face of the console.
#:
#: Duplicated from `app.js` on purpose, and the duplication is bounded: the server
#: needs to know which paths are pages so an unknown one 404s instead of silently
#: serving the console. A test asserts the two lists agree, which is cheaper than
#: a route that generates itself from a JavaScript array.
READ_VIEWS: frozenset[str] = frozenset({"overview", "projects", "sources", "map", "capex"})
DEV_VIEWS: frozenset[str] = frozenset({"pipeline", "commands", "help"})


@lru_cache(maxsize=1)
def deployed_commit() -> str | None:
    """The commit this process is serving, or None when there is no checkout.

    **Which version is in production is a question this system created.** Code
    now reaches the host by a poller rather than by a person, so "is my fix live
    yet?" has no answer at the keyboard — the deploy log is on the far side of an
    ssh, and a restart is not proof that the restart picked up the commit you
    meant.

    Read from `.git` directly rather than by running `git`: this is answered on a
    health check, and a subprocess per request is a cost with no return. Cached,
    because the answer cannot change without the process restarting — the
    deployer restarts it precisely so that it does.
    """
    head = install_root() / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            target = install_root() / ".git" / ref[5:]
            if target.is_file():
                return target.read_text(encoding="utf-8").strip()[:8]
            # A packed ref: the loose file is gone once `git gc` has run.
            packed = install_root() / ".git" / "packed-refs"
            name = ref[5:]
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(f" {name}"):
                    return line.split()[0][:8]
            return None
        return ref[:8]  # detached HEAD
    except OSError:
        return None


class Console:
    """Shared state one server instance hands to every request."""

    def __init__(
        self,
        db_path: Path,
        *,
        allow_write: bool = True,
        allow_ai: bool | None = None,
        password: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.allow_write = allow_write
        #: Whether the LLM panels — the briefing, `infer`, the capex overview —
        #: may run. Separate from `allow_write` because they are a different risk:
        #: they *read* the row and spend tokens, and `tracker infer` has never
        #: written its answer anywhere. Conflating the two made a published
        #: read-only console refuse the one thing it could safely offer.
        #:
        #: Follows `allow_write` unless a deployment says otherwise, so the local
        #: default is unchanged and only a published console has to think about it.
        self.allow_ai = allow_write if allow_ai is None else allow_ai
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
        csp: str | None = None,
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
        #
        # `frame-src 'self' https:` is the single deliberate exception, and it is
        # scoped as narrowly as the feature allows. The sources modal frames two
        # things: our own reader view at `/api/article`, and — behind a second tab
        # — the publisher's live page. Everything else stays shut: scripts, styles,
        # fonts, images and `connect-src` are all still same-origin, so this widens
        # what the page may *display* and nothing it may load or call.
        #
        # **`'self'` is listed explicitly and has to be.** Naming `frame-src` at
        # all replaces the fallback chain to `default-src`, so `frame-src https:`
        # alone silently forbade our own same-origin reader frame — the browser
        # blocked it and the modal came up empty.
        #
        # It buys less than it looks like. Publishers that send `X-Frame-Options`
        # or their own `frame-ancestors` still refuse, and no header of ours can
        # override theirs — see `ArticleModal` in app.js for what is shown instead.
        #
        # **`csp` replaces this policy rather than adding to it, and that is not a
        # convenience.** A response carrying two CSP headers is held to *both*: the
        # browser intersects them. The reader view needs `img-src https:` and the
        # console's policy says `img-src 'self'`, so appending a second header
        # would leave an intersection permitting no images at all — a stricter
        # result than either policy asks for, arrived at silently.
        self.send_header(
            "Content-Security-Policy",
            csp
            or (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "font-src 'self'; connect-src 'self'; frame-src 'self' https:"
            ),
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
        except ImportError as exc:
            self._stale_source(f"GET {route}", exc)
        except Exception:
            log.exception("GET %s failed", route)
            self._error(500, "internal error; see the server log")

    do_HEAD = do_GET

    #: What a lazily-imported module failing to import almost always means here.
    #:
    #: This server reads its Python once, at startup, and its static files and
    #: migrations from disk on every request. Update the tree underneath a running
    #: instance — a merge, a pull — and the process is half of each: modules loaded
    #: at startup stay yesterday's, and any module first imported *after* the change
    #: is loaded fresh from today's files. The two then disagree.
    #:
    #: Observed exactly once and reported as a database fault, which cost an hour:
    #: a console published at 23:19 answered `/api/dataset` with "internal error"
    #: the next day. `capex` had never been imported, so the first request after
    #: the merge loaded the new one, which imports `tracker.pairs`, which imports
    #: `NotDuplicate` from a `tracker.models` that had been in memory since the
    #: previous evening and did not have it. Nothing was wrong with the database;
    #: it was read successfully on the same request.
    #:
    #: An `ImportError` reaching this handler is very nearly diagnostic: every
    #: import this codebase performs at request time is of a module that ships
    #: beside it, so the only ways to fail are a broken install — which would not
    #: have started — or a tree that moved.
    def _stale_source(self, what: str, exc: ImportError) -> None:
        log.exception("%s failed on an import; the source tree changed underneath", what)
        self._error(
            503,
            f"{exc}\n\n"
            "The console's source changed on disk after this process started, so its "
            "modules no longer agree with each other. Nothing is wrong with the "
            "database. Restart the console and this goes away.",
        )

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
        if route in {"/dev", "/dev/"}:
            return self._page(dev=True)
        # One shell per view, so a page can be linked to, refreshed and reached
        # with the back button. The server does not render them differently — it
        # tells the front end which to open, and the front end pushes the same
        # paths as you navigate. Anything else 404s rather than silently serving
        # the console, so a typo is visible instead of landing on Overview.
        page = route.strip("/")
        if page in READ_VIEWS:
            return self._page(view=page)
        if page.startswith("dev/") and page[len("dev/") :] in DEV_VIEWS:
            return self._page(dev=True, view=page[len("dev/") :])
        if route == "/api":
            return self._api_index()
        if route.startswith("/static/"):
            return self._static(route[len("/static/") :], query)
        if route == "/api/dataset":
            return self._dataset()
        if route == "/api/commands":
            from tracker.webui import workflows

            return self._json(
                {
                    "groups": catalog.grouped_json(),
                    "llm": sorted(catalog.LLM_COMMANDS),
                    "destructive": dict(catalog.DESTRUCTIVE),
                    "workflows": workflows.as_json(),
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
        if route == "/api/discover":
            return self._discover()
        if route == "/api/claims":
            return self._claims(query)
        if route == "/api/article":
            return self._article(query)
        if route == "/api/landing":
            return self._landing()
        if route == "/api/health":
            return self._json(
                {"ok": True, "version": __version__, "commit": deployed_commit()}
            )
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
            if parsed.path == "/api/workflow":
                return self._start_workflow(body)
            if parsed.path == "/api/run/cancel":
                return self._json({"cancelled": self.console.runner.cancel()})
            if parsed.path == "/api/infer":
                # `body`, not `self._body()`: the request body was already read
                # above, and reading it twice blocks on `rfile` waiting for bytes
                # that will never come — the request hangs until the client's
                # timeout rather than failing.
                return self._infer(body)
            if parsed.path == "/api/overview":
                return self._overview(body)
            if parsed.path == "/api/overview/stream":
                return self._overview_stream(body)
            if parsed.path == "/api/capex/overview/stream":
                return self._capex_overview_stream(body)
            self._error(404, f"no route {parsed.path!r}")
        except ConnectionError:
            log.debug("POST %s: client went away", parsed.path)
        except ImportError as exc:
            self._stale_source(f"POST {parsed.path}", exc)
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

    def _page(self, *, dev: bool = False, view: str = "") -> None:
        """The console shell. Two faces, one bundle.

        `/` reads the dataset; `/dev` runs commands. The difference is one flag on
        `window.DC_MODE`, which the front end reads to pick its view set — rather
        than a second bundle, because `assets.bundle_css` exists precisely because
        a chain of imports costs a serial round-trip each, and the reading console
        is the one that must not pay for them.

        The flag is a *display* choice and nothing more. What the dev console can
        actually do is still governed by `allow_write` on the server, so
        `serve --no-run` renders `/dev` with every button inert. A page cannot
        grant itself a capability by asking for a different shell.

        `view` is the page the URL asked for, injected as `window.DC_VIEW` so the
        front end opens on it directly. Without it a deep link would paint the
        default view first and then swap, which reads as a flash of the wrong page.
        """
        index = assets.STATIC_ROOT / "index.html"
        if not index.is_file():
            return self._error(
                500,
                f"the console's static files are missing from {assets.STATIC_ROOT}. "
                "This is an incomplete install, not a configuration problem.",
            )
        # Stamped on the way out, so every asset URL carries its file's version.
        # The page itself is `no-store`, so the tokens are never stale.
        html = assets.stamp(index.read_text(encoding="utf-8"))
        mode = "dev" if dev else "read"
        html = html.replace(
            '<div id="root"></div>',
            f'<script>window.DC_MODE="{mode}";window.DC_VIEW="{view}"</script>\n'
            '<div id="root"></div>',
        )
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _static(self, relative: str, query: dict[str, list[str]] | None = None) -> None:
        path = assets.resolve(relative)
        if path is None:
            return self._error(404, f"no asset {relative!r}")

        # A request carrying the right version token can be cached hard, because
        # the URL changes whenever the file does — `index.html` is `no-store`, so
        # a new token always reaches the browser. This used to be `no-cache` for
        # everything, on the reasoning that the files are versioned by content
        # rather than by URL and `immutable` would strand a browser on last
        # month's bundle. Stamping the URLs is what removed that objection; it
        # also removed the cost, which was re-fetching three megabytes of vendored
        # JavaScript on every page load, since nothing here sends an ETag.
        asked = (query or {}).get("v", [""])[0]
        fresh = asked and asked == assets.version_token(path)
        cache = "public, max-age=31536000, immutable" if fresh else "no-cache"
        # A stylesheet is served with its `@import`s already folded in, so the
        # version token on the URL covers every layer rather than only the file
        # that lists them. See `assets.bundle_css`: unversioned children behind a
        # versioned parent is how the console ended up rendering with its form
        # styles missing while everything else was current.
        if path.suffix.lower() == ".css":
            body = assets.bundle_css(path).encode("utf-8")
        else:
            body = path.read_bytes()
        self._send(200, body, assets.content_type(path), cache=cache)

    def _dataset(self) -> None:
        from tracker.webui.dataset import build

        with self.console.read_session() as session:
            payload = build(
                session,
                db_path=str(self.console.db_path),
                schema_version=self.console.schema_version,
            )
        payload["allow_write"] = self.console.allow_write
        payload["allow_ai"] = self.console.allow_ai
        payload["password_protected"] = self.console.gate.required
        self._json(payload)

    def _discover(self) -> None:
        """Stage 1's funnel: what each feed cost and what it returned.

        Its own route rather than a field on `/api/dataset`, because the dataset
        payload is fetched on every redraw and this is a grouped scan of
        `ingest_url` — 6,695 rows today and the fastest-growing table here. The
        panel that reads it is opened deliberately, so it can pay for itself.

        Read-only, like every GET: the console's only route to changing anything
        is to start the CLI.
        """
        from tracker import funnel

        with self.console.read_session() as session:
            self._json(funnel.survey(session).as_json())

    #: Every route, what it answers, and what it costs. Hand-written on purpose,
    #: for the reason `catalog.GROUPS` is: a derived list describes the code, and
    #: what a caller needs is what the route is *for*. A test asserts that every
    #: route the handler dispatches appears here, so it cannot silently rot.
    API: ClassVar[dict[str, dict[str, Any]]] = {
        "GET /api": {"answers": "this index", "reads": None},
        "GET /api/health": {"answers": "liveness and version", "reads": None},
        "GET /api/article": {
            "answers": "the text behind one cited URL, with its stored quotes located in it",
            "reads": "the article cache, and the network on a miss",
            "note": "?url=<url>. Refused unless that URL is already cited in the "
            "database. Ten of the fifteen most-cited publishers refuse to be framed, "
            "so the modal reads our own copy rather than the live page.",
        },
        "GET /api/claims": {
            "answers": "every claim any citation made about one project, field by field",
            "reads": "one project and its sources",
            "note": "?project=<id>. Split out of /api/dataset, where it was 48% of 19 MB",
        },
        "GET /api/dataset": {
            "answers": "every project with its provenance, plus capex, gaps, queue and totals",
            "reads": "the whole database",
            "note": "refetched after every run. Excludes claims_by_field — see /api/claims",
        },
        "GET /api/landing": {
            "answers": "evidence census, clean tiers, and what is waiting on a person",
            "reads": "quality.evidence_census + clean.scan + sources.survey",
            "note": "~2.5s. Its own route so /api/dataset stays fast.",
        },
        "GET /api/discover": {
            "answers": "per-feed funnel: queued, read, no_project, cited, dated",
            "reads": "ingest_url",
        },
        "GET /api/commands": {
            "answers": "every command, its flags and their types, plus the workflows",
            "reads": None,
            "note": "the palette is built from this; a flag added to the CLI appears here",
        },
        "GET /api/runs": {"answers": "run history and the current run", "reads": "data/runs"},
        "GET /api/run/<id>": {"answers": "one run's log", "reads": "data/runs"},
        "GET /api/run/<id>/stream": {"answers": "that log as it is written", "reads": "data/runs"},
        "POST /api/run": {
            "answers": "starts a command; returns its run id",
            "writes": True,
            "note": "refused unless the server was started with --run. "
            "Body: {cmd, flags} or {workflow} or {line}. Validated against the catalog.",
        },
        "POST /api/workflow": {
            "answers": "starts a named routine; returns its run id",
            "writes": True,
            "note": "each step validated against the catalog, so a blocked command "
            "cannot be reached by putting it in a sequence",
        },
        "POST /api/run/cancel": {"answers": "cancels the running command", "writes": True},
        "POST /api/queue/drop": {"answers": "drops queued URLs", "writes": True},
        "POST /api/infer": {"answers": "one project's inferred analysis", "writes": True},
        "POST /api/overview": {"answers": "one project's AI reading", "writes": True},
        "POST /api/overview/stream": {
            "answers": "that reading as it is generated",
            "writes": True,
        },
        "POST /api/capex/overview/stream": {
            "answers": "an AI reading of one capex position, streamed",
            "writes": True,
        },
        "POST /api/login": {"answers": "exchanges the password for a session cookie"},
        "POST /api/logout": {"answers": "clears it"},
    }

    def _api_index(self) -> None:
        """The route map, so a caller does not have to read `_route_get`.

        Written for an agent as much as for a person: the console is driven from a
        terminal as often as from a browser, and "what can I ask this server?" had
        no answer short of reading the source.
        """
        self._json(
            {
                "service": "dc-tracker console",
                "version": __version__,
                "consoles": {
                    "/": "read the dataset — overview, projects, map, capex",
                    "/dev": "run things — pipeline, commands, help",
                },
                "allow_write": self.console.allow_write,
                "allow_ai": self.console.allow_ai,
                "routes": self.API,
            }
        )

    def _claims(self, query: dict[str, list[str]]) -> None:
        """Every claim any citation made about one project, field by field.

        Split out of `/api/dataset` because it was **48% of a 19 MB payload** —
        9.2 MB shipped on every load for a table that renders one project at a
        time, inside a drawer most visits never open. One project's worth is a few
        kilobytes and arrives while the drawer is animating.
        """
        raw = (query.get("project") or [""])[0]
        try:
            project_id = int(raw)
        except ValueError:
            return self._error(400, "project must be an integer id")

        from tracker.export import claims_for

        with self.console.read_session() as session:
            payload = claims_for(session, project_id)
        if payload is None:
            return self._error(404, f"no project {project_id}")
        self._json({"project": project_id, "claims_by_field": payload})

    def _article(self, query: dict[str, list[str]]) -> None:
        """Reader view of one cited article, for the sources modal's frame.

        Serves a whole document rather than JSON because that is what the frame
        loads. Same-origin, so the parent's `default-src 'self'` already permits
        it; the document then carries its own `default-src 'none'`, and the frame
        that holds it carries `sandbox` with no `allow-` tokens.

        **The console reads a page the pipeline already chose, and nothing else.**
        `article.load` refuses any URL that is not a stored `source.url` — the
        allowlist is the database itself. Without that rule a console reachable
        from a network is a request forwarder aimed at whatever sits behind it,
        and being read-only says nothing about where it may be pointed.

        A cache miss costs one ordinary request and is written back, so the second
        reader of an article waits for nothing. It writes a file, never a row.
        """
        url = (query.get("url") or [""])[0].strip()
        if not url:
            return self._error(400, "url is required")
        if urlsplit(url).scheme not in {"http", "https"}:
            return self._error(400, "url must be http or https")

        from tracker.config import install_root
        from tracker.webui import article as article_mod

        root = install_root() / ".cache"
        with self.console.read_session() as session:
            found = article_mod.load(
                session, url, cache_dir=root / "articles", reader_dir=root / "reader"
            )
        if found.error and not found.body:
            return self._error(404, found.error)
        dark = (query.get("theme") or [""])[0] == "dark"
        page = article_mod.render(found, dark=dark).encode("utf-8")
        self._send(
            200,
            page,
            "text/html; charset=utf-8",
            # Its own policy, not the console's: this document is somebody else's
            # markup. Nothing may load except images, and no script at all — which
            # is the third of three independent guards, after sanitising the HTML
            # and sandboxing the frame that holds it.
            csp="default-src 'none'; img-src https: data:; style-src 'unsafe-inline'",
        )

    def _landing(self) -> None:
        """What the landing page needs to answer "can I trust these numbers?".

        Named `_landing`, not `_overview`: `/api/overview` is already the POST that
        writes a project's AI reading, and a second `_overview` silently shadowed
        it — Python keeps the last definition, so the *write* path was the one that
        survived and every GET arrived at a handler expecting a body.

        **Its own route, and that is not a style choice.** Measured on the live
        database: `quality.evidence_census` 1.0s, `clean.scan` 2.5s,
        `sources.survey` 0.24s. `/api/dataset` is refetched on every redraw and
        after every run, so putting a 3.8-second scan in it would make the whole
        console slower to answer a question only one view asks. Here it is fetched
        once, when the Overview opens, and the page renders its cheap bands from
        the dataset it already has while this arrives.

        Nothing is computed here. The census, the tier sweep and the attention list
        are the same functions the CLI calls, for the reason
        `docs/architecture.md` states: the console makes no judgements of its own.
        """
        from tracker import clean, quality, sources

        with self.console.read_session() as session:
            census = quality.evidence_census(session)
            sweep = clean.scan(session)
            survey = sources.survey(session)

        top = survey.ranked(by="decisive")[:8]
        self._json(
            {
                "evidence": {
                    "total": census.total,
                    "buckets": dict(census.buckets),
                    # Pre-divided, so the page cannot disagree with `tracker stats`
                    # about what "quote-backed" means.
                    "quote_backed": census.buckets.get(quality.QUOTE_BACKED, 0),
                    "quote_backed_share": round(census.share(quality.QUOTE_BACKED), 4),
                    "defects": census.defects,
                    "by_field": census.by_field,
                },
                "tiers": sweep.as_json(),
                # Ordered, because the tier bar is a *sequence* and a dict of
                # counts does not carry the order. -1 is a real rung — "not even
                # sourced" — and is deliberately not folded into 0.
                "tier_names": [[-1, "UNSOURCED"], *[[n, name] for n, name, _ in clean.TIERS]],
                "attention": clean.attention(sweep, limit=8),
                "sources": {
                    "publishers": len(survey.hosts),
                    "citations": survey.sources_read,
                    "decisions": survey.decisions,
                    "contested": survey.contested,
                    "top": [h.as_json() for h in top],
                },
            }
        )

    def _start_run(self, body: dict[str, Any]) -> None:
        if not self.console.allow_write:
            return self._error(403, "this console was started read-only (--no-run)")

        # `line` is the console's command box. It is parsed *here*, against the
        # catalog, into exactly the `(cmd, flags)` the form produces — so the box
        # is a shorthand for the form and not a second, weaker way in. There is no
        # shell at any point: `cd`, `rm`, `;` and `|` are words the catalog does
        # not know, and it says so by name.
        if body.get("line"):
            try:
                cmd, flags = catalog.parse_command_line(str(body["line"]))
            except catalog.InvalidRequest as exc:
                return self._error(400, str(exc))
        else:
            cmd = str(body.get("cmd") or "").strip()
            flags = body.get("flags") or {}
        if not isinstance(flags, dict):
            return self._error(400, "`flags` must be an object")
        try:
            run = self.console.runner.start(cmd, flags, confirm=body.get("confirm"))
        except catalog.InvalidRequest as exc:
            # When the only thing missing is the confirmation, say which word
            # confirms it. The command box has no form to read that off, and
            # "re-send with confirm=..." is a sentence about an HTTP API rather
            # than an instruction to a person.
            command = catalog.by_name().get(cmd)
            if command is not None and command.needs_confirmation and not body.get("confirm"):
                return self._json(
                    {"error": str(exc), "confirm_with": cmd, "destroys": command.destroys},
                    status=400,
                )
            return self._error(400, str(exc))
        except Busy as exc:
            return self._error(409, str(exc))
        self._json({"run": run.summary()}, status=202)

    def _start_workflow(self, body: dict[str, Any]) -> None:
        if not self.console.allow_write:
            return self._error(403, "this console was started read-only (--no-run)")
        name = str(body.get("name") or "").strip()
        try:
            run = self.console.runner.start_workflow(name, confirm=body.get("confirm"))
        except catalog.InvalidRequest as exc:
            return self._error(400, str(exc))
        except Busy as exc:
            return self._error(409, str(exc))
        self._json({"run": run.summary()}, status=202)

    def _infer(self, body: dict[str, Any]) -> None:
        """Run `tracker infer` for one project and return it as structured JSON.

        A POST, and gated on a confirmation string, for the same reason the
        briefing is: it spends LLM tokens, and a GET that spends money is a GET a
        browser will re-issue on a back button.

        **Deliberately not cached and deliberately not stored.** The briefing is
        cached by content fingerprint because it describes the row as it stands;
        an inference is a judgement about what might go wrong next, its value is
        that somebody asked for it just now, and `tracker infer` has never written
        one to the database. So the panel behind it is a button, not something
        that runs when a drawer opens — the one place in this console where a cost
        is paid only on a deliberate click.

        The response shape is `infer.Analysis` with nothing added: obstacles,
        signals, whatever the model tried to assert and was refused, and the model
        that said it. The refusals are included rather than dropped because a
        model reaching for `investment_usd` is something the operator should see.
        """
        from tracker.infer import analyse
        from tracker.models import Project

        try:
            project_id = int(body.get("project_id"))
        except (TypeError, ValueError):
            return self._error(400, "project_id must be an integer")

        with self.console.read_session() as session:
            project = session.get(Project, project_id)
            if project is None:
                return self._error(404, f"no project #{project_id}")

            if not self.console.allow_ai:
                return self._error(403, "this console was started with --no-ai")
            if str(body.get("confirm") or "").strip() != "infer":
                return self._error(
                    400, 'Running an inference spends LLM tokens. Re-send with confirm="infer".'
                )

            from tracker.config import get_settings
            from tracker.llm import MissingApiKey, reasoning_extractor

            try:
                extractor = reasoning_extractor(get_settings())
            except MissingApiKey as exc:
                return self._error(503, str(exc))

            analysis = analyse(project, extractor=extractor)
            payload = {
                "project_id": project.id,
                "model": analysis.model,
                "rejected": list(analysis.rejected),
                "obstacles": [
                    {
                        "category": r.category,
                        "severity": r.severity,
                        "confidence": round(r.confidence, 2),
                        "reasoning": r.reasoning,
                    }
                    for r in analysis.obstacles
                ],
                "signals": [
                    {
                        "signal": s.signal,
                        "confidence": round(s.confidence, 2),
                        "reasoning": s.reasoning,
                    }
                    for s in analysis.signals
                ],
            }
        if analysis.empty:
            payload["empty"] = True
        self._json(payload)

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

            if not self.console.allow_ai:
                return self._error(403, "this console was started with --no-ai")
            if str(body.get("confirm") or "").strip() != "overview":
                return self._error(
                    400, 'Writing a briefing spends LLM tokens. Re-send with confirm="overview".'
                )

            from tracker.config import get_settings
            from tracker.llm import MissingApiKey, fast_extractor

            try:
                extractor = fast_extractor(get_settings())
            except MissingApiKey as exc:
                return self._error(503, str(exc))

            written = overview_mod.write(project, extractor=extractor)

        if written is None:
            return self._error(502, "the model did not return a usable briefing")
        self._json({**written.as_json(), "cached": False})

    def _overview_stream(self, body: dict[str, Any]) -> None:
        """The briefing, sent as it is written.

        Server-sent events over a POST, which `EventSource` cannot do — the client
        reads the body with `fetch`. Worth the small awkwardness: the alternative
        is a GET that spends money, and a browser will re-issue a GET on a back
        button without asking anybody.

        A cached briefing is sent as one frame. Nothing is generated twice, and the
        panel does not perform a typewriter animation over text it already had.
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
                return self._send(
                    200,
                    (
                        _sse({"type": "text", "text": ready.text})
                        + _sse({"type": "end", **ready.as_json(), "cached": True})
                    ).encode("utf-8"),
                    "text/event-stream; charset=utf-8",
                )

            if not self.console.allow_ai:
                return self._error(403, "this console was started with --no-ai")
            if str(body.get("confirm") or "").strip() != "overview":
                return self._error(
                    400, 'Writing a briefing spends LLM tokens. Re-send with confirm="overview".'
                )

            from tracker.config import get_settings
            from tracker.llm import MissingApiKey, fast_extractor

            try:
                extractor = fast_extractor(get_settings())
            except MissingApiKey as exc:
                return self._error(503, str(exc))

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            wrote = False
            for piece in overview_mod.stream(project, extractor=extractor):
                wrote = True
                self.wfile.write(_sse({"type": "text", "text": piece}).encode("utf-8"))
                self.wfile.flush()

            done = overview_mod.cached(project)
            tail = (
                {"type": "end", **done.as_json(), "cached": False}
                if done is not None
                else {"type": "error", "error": "the model did not return a usable briefing"}
            )
            if not wrote and done is None:
                tail = {"type": "error", "error": "the model returned nothing"}
            self.wfile.write(_sse(tail).encode("utf-8"))
            self.wfile.flush()

    def _capex_overview_stream(self, body: dict[str, Any]) -> None:
        """A buyer-position briefing for the capex table's hover card.

        Same contract as `_overview_stream` — POST because it can spend, a
        confirm token, cached briefings sent as one frame — over a derived
        subject: the position is recomputed from the database on every request,
        so the reading can never describe a rollup the table is not showing.
        """
        from tracker import capex as capex_mod
        from tracker import overview as overview_mod
        from tracker.models import Project

        if "key" not in body:
            return self._error(400, "key is required (the position's buyer key; empty is valid)")
        key = str(body.get("key") or "")

        with self.console.read_session() as session:
            positions = capex_mod.rollup(session)
            position = next((p for p in positions if p.key == key), None)
            if position is None:
                return self._error(404, f"no buyer position {key!r}")
            projects = [
                row
                for pid in position.project_ids
                if (row := session.get(Project, pid)) is not None
            ]

            ready = overview_mod.cached_position(position, projects)
            if ready is not None:
                return self._send(
                    200,
                    (
                        _sse({"type": "text", "text": ready.text})
                        + _sse({"type": "end", **ready.as_json(), "cached": True})
                    ).encode("utf-8"),
                    "text/event-stream; charset=utf-8",
                )

            if not self.console.allow_ai:
                return self._error(403, "this console was started with --no-ai")
            if str(body.get("confirm") or "").strip() != "overview":
                return self._error(
                    400, 'Writing a briefing spends LLM tokens. Re-send with confirm="overview".'
                )

            from tracker.config import get_settings
            from tracker.llm import MissingApiKey, fast_extractor

            try:
                extractor = fast_extractor(get_settings())
            except MissingApiKey as exc:
                return self._error(503, str(exc))

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            wrote = False
            for piece in overview_mod.stream_position(position, projects, extractor=extractor):
                wrote = True
                self.wfile.write(_sse({"type": "text", "text": piece}).encode("utf-8"))
                self.wfile.flush()

            done = overview_mod.cached_position(position, projects)
            tail = (
                {"type": "end", **done.as_json(), "cached": False}
                if done is not None
                else {"type": "error", "error": "the model did not return a usable briefing"}
            )
            if not wrote and done is None:
                tail = {"type": "error", "error": "the model returned nothing"}
            self.wfile.write(_sse(tail).encode("utf-8"))
            self.wfile.flush()

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
    allow_ai: bool | None = None,
    password: str | None = None,
) -> None:
    """Run the console until interrupted."""
    console = Console(db_path, allow_write=allow_write, allow_ai=allow_ai, password=password)
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
