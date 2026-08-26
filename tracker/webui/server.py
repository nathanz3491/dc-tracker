"""The HTTP surface: static files, a read-only data API, and the sign-in routes.

Routing is written out rather than derived, because the read/write split is the
security posture and it should be readable in one screen.

**Exactly one route writes anything: ``POST /api/watch``.** Everything else under
``/api/`` opens the database ``mode=ro``, so a bug in a read path raises instead of
silently changing a row. And that one write is as narrow as a write gets — it adds
or drops a row of `watch`, which says whose news the landing page shows, which
nothing derives from and no ingest consults.

This console used to be able to run commands: a palette read out of the CLI, a
`/dev` face, and a real subprocess per button. That is gone. The database is
changed from the CLI, by one person, on the host — so keeping a command runner
behind a public URL meant keeping its three doors (the typed-name confirmation, the
single-writer check, the argv-never-a-string rule) correct forever in exchange for
a feature with no users. `tracker tui` is where the buttons live now, and it still
shares `webui/catalog.py` and `webui/runner.py`, which is why those modules are
still here.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
import webbrowser
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from sqlalchemy.exc import OperationalError

from tracker import __version__
from tracker.config import install_root
from tracker.db import MigrationError, open_db, schema_version, session_scope
from tracker.webui import assets
from tracker.webui.auth import COOKIE, Gate, cookie_value

log = logging.getLogger(__name__)

#: Loopback only unless the operator overrides it deliberately.
#:
#: This process no longer executes commands, but the bind address is still a
#: security boundary rather than a convenience setting: what is behind it is the
#: whole dataset and, with `--ai`, a model panel that spends real tokens per click.
#: An open console on a routable address is those two things offered to the network.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Below this, compressing costs more than it saves.
GZIP_MIN = 8192

#: How long `Console.auth_required` may go without re-reading the database. See
#: that property for why it is cached at all and why the window is this short.
AUTH_CACHE_S = 5.0

#: The views that have their own URL. One face now, where there used to be two.
#:
#: Duplicated from `app.js` on purpose, and the duplication is bounded: the server
#: needs to know which paths are pages so an unknown one 404s instead of silently
#: serving the console. A test asserts the two lists agree, which is cheaper than
#: a route that generates itself from a JavaScript array.
#:
#: `help` arrives here from the old `/dev` set, and belongs here: it explains tiers,
#: tracks and confidence — what a reader needs in order not to misread the data —
#: and was only filed under the machinery because that is where the tab happened to
#: sit.
READ_VIEWS: frozenset[str] = frozenset({"updates", "projects", "sources", "map", "capex", "help"})


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
        allow_ai: bool = True,
        allow_watch: bool = True,
    ) -> None:
        self.db_path = db_path
        #: Whether the LLM panels — the briefing, `infer`, the capex overview —
        #: may run. They *read* a row and spend a token; `tracker infer` has never
        #: written its answer anywhere, which is why spending and writing were two
        #: flags rather than one even when this console could still write.
        self.allow_ai = allow_ai
        #: Whether the watchlist may be edited from the page.
        #:
        #: This is now the console's **only** write, and it is a narrow one: add or
        #: drop a row of `watch`, a statement about whose news to show, which no
        #: derived value reads and no ingest consults. Losing the table would lose a
        #: preference rather than a fact. `serve --no-watch-edits` turns it off for
        #: a deployment that wants the page strictly read-only.
        #:
        #: It still requires a session, and now requires more than that: an
        #: anonymous visitor to an open console has no list to edit, because a
        #: watchlist without an owner is the shared list that accounts exist to
        #: replace. See `Handler._watch`.
        self.allow_watch = allow_watch
        self.gate = Gate()
        self._schema_version: int | None = None
        self._auth_required = False
        self._auth_checked_at = 0.0

    def read_session(self):
        engine = open_db(self.db_path)
        if self._schema_version is None:
            self._schema_version = schema_version(engine)
        return session_scope(engine, commit=False)

    @property
    def schema_version(self) -> int:
        return self._schema_version or 0

    @property
    def auth_required(self) -> bool:
        """Whether anybody has to sign in — i.e. whether any account exists.

        Read from the database rather than from a flag, because the answer is made
        by `tracker users add` in a *different process* and a console that had been
        told at startup would keep serving an open page until somebody restarted
        it. That is the trap this property exists to avoid.

        Cached for a few seconds, because `_authed` runs on every request including
        every static asset, and opening the database per file would be a real cost
        for an answer that changes about once. The staleness window is shorter than
        the time it takes to alt-tab to the browser after creating an account.

        A database that cannot be read at all is treated as **requiring** auth. The
        request is going to fail anyway, and the safe direction for a doubt about
        whether a password is needed is "yes".
        """
        now = time.monotonic()
        if now - self._auth_checked_at < AUTH_CACHE_S:
            return self._auth_required
        try:
            with self.read_session() as session:
                from tracker import accounts

                self._auth_required = accounts.any_exist(session)
        except Exception:  # a missing file, a pending migration, a locked database
            log.debug("console: could not count accounts; assuming a sign-in is needed")
            self._auth_required = True
        self._auth_checked_at = now
        return self._auth_required


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
    def _account_id(self) -> int | None:
        """Which account this request is, or None for nobody.

        None means two different things and the caller has to know which: on a
        console with no accounts it means "anonymous, and that is allowed"; on one
        with accounts it means "not signed in". `_authed` is the question that
        distinguishes them, and it is the only one the routing asks.
        """
        return self.console.gate.session_for(self._session)

    @property
    def _authed(self) -> bool:
        """Whether this request may be served at all.

        An open console — no accounts at all — serves everybody, exactly as an
        unset `TRACKER_CONSOLE_PASSWORD` did before accounts existed: reaching
        loopback already means having the machine, and publishing is what refuses
        (see `cli._console_accounts`). Once one account exists, every route needs
        a session.
        """
        if not self.console.auth_required:
            return True
        return self._account_id is not None

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
        # One shell per view, so a page can be linked to, refreshed and reached
        # with the back button. The server does not render them differently — it
        # tells the front end which to open, and the front end pushes the same
        # paths as you navigate. Anything else 404s rather than silently serving
        # the console, so a typo is visible instead of landing on Updates.
        page = route.strip("/")
        if page in READ_VIEWS:
            return self._page(view=page)
        if route == "/api":
            return self._api_index()
        if route.startswith("/static/"):
            return self._static(route[len("/static/") :], query)
        if route == "/api/dataset":
            return self._dataset()
        if route == "/api/claims":
            return self._claims(query)
        if route == "/api/article":
            return self._article(query)
        if route == "/api/publishers":
            return self._publishers()
        if route == "/api/updates":
            return self._updates(query)
        if route == "/api/health":
            return self._json({"ok": True, "version": __version__, "commit": deployed_commit()})
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
            # The two unauthenticated POSTs, and the only two there will be. Both
            # go through the gate's lockout counters — see `_login` and
            # `_register` — because an unauthenticated route that either hands out
            # a session or writes a row is exactly what a rate limit is for.
            if parsed.path == "/api/login":
                return self._login(body)
            if parsed.path == "/api/register":
                return self._register(body)
            if not self._authed:
                return self._error(401, "sign in first")
            if parsed.path == "/api/logout":
                self.console.gate.revoke(self._session)
                return self._json({"ok": True}, extra=self._set_session_cookie(None))
            # `body` everywhere below, never `self._body()`: the request body was
            # already read above, and reading it twice blocks on `rfile` waiting
            # for bytes that will never come — the request hangs until the
            # client's timeout rather than failing. The watchlist route was
            # written with a second `self._body()` and hung exactly like that.
            if parsed.path == "/api/watch":
                return self._watch(body)
            if parsed.path == "/api/infer":
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

    def _locked_out(self) -> dict[str, Any] | None:
        """The 429 body if this client may not try right now, else None.

        Says it is a lockout and for how long. Answering "wrong password" here
        would have somebody who mistyped twice sit there retyping a correct
        password and never getting in.
        """
        remaining = self.console.gate.locked_for(self._client())
        if not remaining:
            return None
        minutes = max(1, round(remaining / 60))
        return {"error": f"Too many attempts. Locked for about {minutes} more minute(s)."}

    def _login(self, body: dict[str, Any]) -> None:
        """Exchange an email and a password for a session cookie.

        On a console with no accounts there is nothing to sign in to, and saying so
        is better than a bare 401: it is a legitimate state (`tracker serve` on
        loopback needs no setup) and the page needs to know not to show a form.
        """
        if not self.console.auth_required:
            return self._json({"ok": True, "note": "this console has no accounts"})

        locked = self._locked_out()
        if locked:
            return self._json(locked, status=429)

        from tracker import accounts

        client = self._client()
        gate = self.console.gate
        # Read-write, and only because a successful sign-in stamps `last_seen_at`.
        # A failed one writes nothing, which is what keeps a brute-force attempt
        # from being a write per guess.
        engine = open_db(self.console.db_path, readonly=False)
        with session_scope(engine) as session:
            account = accounts.verify(
                session, str(body.get("email") or ""), str(body.get("password") or "")
            )
            if account is None:
                gate.fail(client)
                log.warning("console: failed sign-in from %s", client)
                # One message for a wrong password and for an unknown address.
                # `accounts.verify` already spends the same scrypt either way, so
                # neither the wording nor the timing says which addresses exist.
                return self._json({"error": "Wrong email or password."}, status=401)
            accounts.touch(session, account)
            account_id = account.id
            email = account.email

        gate.succeed(client)
        token = gate.grant(account_id)
        log.info("console: %s signed in from %s", email, client)
        self._json({"ok": True}, extra=self._set_session_cookie(token))

    def _register(self, body: dict[str, Any]) -> None:
        """Spend an invite code and sign the new account straight in.

        The one route that creates an identity from the browser, and it can only do
        so with a code minted at a terminal by `tracker users invite`. Open
        registration would hand an account to anyone who finds the URL, and while
        an account can no longer run a command it can still read the whole dataset.

        Rate limited by the **same counters as `_login`**, deliberately shared: two
        separate budgets would let somebody exhaust one while guessing at the other,
        and a code is guessable in exactly the sense a password is (it is not, at
        160 bits — but the counter is what makes that claim true rather than
        assumed).
        """
        locked = self._locked_out()
        if locked:
            return self._json(locked, status=429)

        from tracker.accounts import AccountError, redeem

        client = self._client()
        engine = open_db(self.console.db_path, readonly=False)
        try:
            with session_scope(engine) as session:
                account = redeem(
                    session,
                    str(body.get("code") or ""),
                    str(body.get("email") or ""),
                    str(body.get("password") or ""),
                    name=(str(body["name"]).strip() or None) if body.get("name") else None,
                )
                account_id, email = account.id, account.email
        except AccountError as exc:
            self.console.gate.fail(client)
            log.warning("console: refused registration from %s: %s", client, exc)
            return self._json({"error": str(exc)}, status=400)
        except OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            return self._json(
                {"error": "the database is busy writing; try again in a moment"}, status=503
            )

        # Signed in on the spot. Making somebody type the password they just chose
        # into a second form teaches them nothing and is one more place to fail.
        self.console.gate.succeed(client)
        token = self.console.gate.grant(account_id)
        log.info("console: %s registered from %s", email, client)
        self._json({"ok": True}, extra=self._set_session_cookie(token))

    def _page(self, *, view: str = "") -> None:
        """The console shell. One face now.

        There used to be two — `/` read the dataset and `/dev` ran commands, chosen
        by a `window.DC_MODE` flag on one bundle. The runner is gone, so the flag
        and the second view set went with it, and what is left is a reader.

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
        html = html.replace(
            '<div id="root"></div>',
            f'<script>window.DC_VIEW="{view}"</script>\n<div id="root"></div>',
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

        # One session for both, because `read_session` opens the database each
        # time it is called and this is the request every redraw makes.
        with self.console.read_session() as session:
            payload = build(
                session,
                db_path=str(self.console.db_path),
                schema_version=self.console.schema_version,
            )
            # Who is reading, so the header can say so and the watchlist knows
            # whether it has an owner. None on an open console with no accounts,
            # which the page reads as "no watchlist here" rather than "signed out".
            #
            # Resolved before `allow_watch`, because that flag depends on it.
            account = self._account_json(session)
        payload["allow_ai"] = self.console.allow_ai
        payload["auth_required"] = self.console.auth_required
        payload["account"] = account
        # **`allow_watch` answers "may *this reader* edit a watchlist", not "is the
        # feature on".** A visitor with no account has no list to edit — `_watch`
        # refuses them — so sending the server's bare flag would draw an editable
        # box that 403s on use. The same rule is applied in `_updates`, and it has
        # to be applied in both: whichever payload the page believed would
        # otherwise decide, and they would disagree.
        payload["allow_watch"] = self.console.allow_watch and account is not None
        self._json(payload)

    def _account_json(self, session: Any) -> dict[str, Any] | None:
        """The signed-in account as the page needs it, or None for nobody.

        Takes the caller's session rather than opening one, because its only
        caller is `/api/dataset` — the request every redraw makes — and
        `read_session` opens the database each time it is called.

        Also the place a **deleted** account's session dies. `tracker users rm`
        runs in another process and cannot reach the gate's dictionary, so the row
        going missing is what invalidates the cookie — see the note in
        `webui/auth.py` about what this does and does not cover.
        """
        account_id = self._account_id
        if account_id is None:
            return None
        from tracker.models import Account

        row = session.get(Account, account_id)
        if row is None:
            self.console.gate.revoke(self._session)
            return None
        return {"email": row.email, "name": row.name}

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
        "GET /api/publishers": {
            "answers": "which publishers actually decide a stored value",
            "reads": "sources.survey",
            "note": "~0.24s. Its own route so /api/dataset stays fast.",
        },
        "GET /api/updates": {
            "answers": "what changed on the watchlist, signed good or bad, most material first",
            "reads": "feed.digest — every project with its events, risks and citations",
            "note": "?days=<n> or ?since=<iso>, ?limit=<n>. The window is on when we "
            "learned a fact, not when it happened: see tracker/feed.py.",
        },
        "POST /api/watch": {
            "answers": "adds or drops one watchlist entry; returns the list",
            "writes": True,
            "note": "Body: {action: add|remove, entry, note?}. The ONLY route here "
            "that writes, because a watch is a statement about whose news to show "
            "and nothing derives from it. Acts on the signed-in account's list, so "
            "it needs an account and not merely a session. Refused under "
            "--no-watch-edits.",
        },
        # The three that spend a token and write nothing. `tracker infer` has never
        # stored its answer anywhere, which is why spending and writing were two
        # separate flags even when this console could still do both.
        "POST /api/infer": {"answers": "one project's inferred analysis", "spends": True},
        "POST /api/overview": {"answers": "one project's AI reading", "spends": True},
        "POST /api/overview/stream": {
            "answers": "that reading as it is generated",
            "spends": True,
        },
        "POST /api/capex/overview/stream": {
            "answers": "an AI reading of one capex position, streamed",
            "spends": True,
        },
        "POST /api/login": {"answers": "exchanges an email and password for a session cookie"},
        "POST /api/register": {
            "answers": "spends an invite code, creates the account, signs it in",
            "writes": True,
            "note": "Body: {code, email, password, name?}. Unauthenticated by "
            "necessity and rate limited by the same counters as /api/login. Codes "
            "come from `tracker users invite`, at a terminal.",
        },
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
                "note": (
                    "a reader. Nothing here changes a project, a citation or a "
                    "figure — that is the CLI's job. `tracker tui` has the commands."
                ),
                "allow_ai": self.console.allow_ai,
                "allow_watch": self.console.allow_watch,
                "auth_required": self.console.auth_required,
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

    def _publishers(self) -> None:
        """Which publishers actually decide a stored value.

        **Its own route, and that is not a style choice.** `/api/dataset` is
        refetched on every redraw and after every run, so a scan belongs anywhere
        but there. Measured on the live database: `sources.survey` 0.24s. Fetched
        once, when Sources opens, and the page renders what it already has from the
        dataset while this arrives.

        Named `_publishers` rather than `_landing`, because it no longer answers
        for the landing page. That page used to ask "can I trust these numbers?"
        and answered it with an evidence census and a tier sweep costing 3.5
        seconds between them; it is now the Updates page, and the census and the
        sweep are `tracker stats` and `tracker clean`, where they always also were.
        A route named for a page it does not serve is how a name goes stale.

        Nothing is computed here. The survey is the same function the CLI calls,
        for the reason `docs/architecture.md` states: the console makes no
        judgements of its own.
        """
        from tracker import sources

        with self.console.read_session() as session:
            survey = sources.survey(session)

        top = survey.ranked(by="decisive")[:8]
        self._json(
            {
                "sources": {
                    "publishers": len(survey.hosts),
                    "citations": survey.sources_read,
                    "decisions": survey.decisions,
                    "contested": survey.contested,
                    "top": [h.as_json() for h in top],
                },
            }
        )

    def _updates(self, query: dict[str, list[str]]) -> None:
        """What changed on the watchlist, and whether it was good news.

        The landing page. Everything on it is derived — `tracker.feed` stores
        nothing — so this route is a read like any other and is safe on a console
        started `--no-run`.

        **Not folded into `/api/dataset`.** The dataset already ships every
        project's events and risks, so a page *could* assemble this in the
        browser, and an earlier draft did. Two things decided against it: the
        window has to be applied to `created_at`, which means re-deriving in
        JavaScript the one rule this feature exists to get right, and
        `feed.digest` calls `tracks.standing` per project to find what a blocked
        track was waiting for — the judgement `docs/architecture.md` says the
        console must never make for itself.

        `?days=` and `?since=` are the same knob, and `?since=` wins when both are
        given. An unparseable value is a 400 rather than a silent fallback to a
        week, because "no updates" and "your filter was ignored" look identical on
        the page.
        """
        import datetime as dt

        from tracker import feed

        since: dt.datetime | None = None
        raw = (query.get("since") or [""])[0]
        if raw:
            try:
                since = dt.datetime.fromisoformat(raw)
            except ValueError:
                return self._error(400, f"since must be an ISO date or datetime, not {raw!r}")

        days = feed.DEFAULT_DAYS
        raw_days = (query.get("days") or [""])[0]
        if raw_days:
            try:
                days = int(raw_days)
            except ValueError:
                return self._error(400, f"days must be a whole number, not {raw_days!r}")
            if days < 1:
                return self._error(400, "days must be at least 1")

        limit: int | None = None
        raw_limit = (query.get("limit") or [""])[0]
        if raw_limit:
            try:
                limit = max(1, int(raw_limit))
            except ValueError:
                return self._error(400, f"limit must be a whole number, not {raw_limit!r}")

        # One session for both, because both walk the same projects: the digest to
        # find what changed, the watchlist to say which entry each one came from.
        #
        # `account_id` is this reader's, so two people on one console get two
        # different pages. None — nobody signed in, on a console with no accounts
        # at all — reaches `feed.digest`'s existing "no watchlist, read everything"
        # path, so the anonymous case needed no branch of its own.
        account_id = self._account_id
        with self.console.read_session() as session:
            payload = feed.digest(
                session, since=since, days=days, limit=limit, account_id=account_id
            ).as_json()
            payload["watchlist"] = self._watchlist_json(session, account_id)

        payload["days"] = days
        payload["allow_watch"] = self.console.allow_watch and account_id is not None
        self._json(payload)

    def _watchlist_json(self, session: Any, account_id: int | None) -> list[dict[str, Any]]:
        """One account's watchlist as the page renders it, against today's projects.

        The page needs more than the digest's per-entity tally: the note, and the
        project count, so an entry matching nothing can say so rather than looking
        like a quiet week.

        Empty for `account_id=None`, and that is not the same as "no entries". A
        visitor to an open console has no list because there is nobody to own one;
        the page reads `account` from `/api/dataset` to tell the two apart.
        """
        if account_id is None:
            return []
        from tracker import watchlist

        return [entity.as_json() for entity in watchlist.watched(session, account_id=account_id)]

    def _watch(self, body: dict[str, Any]) -> None:
        """Add or drop one entry on the signed-in account's watchlist.

        **The one write this console performs**, and the narrowness is the argument
        for it. `watch` rows say whose news to show; nothing derives from them, no
        ingest reads them, and dropping the table would lose a preference rather
        than a fact.

        **It needs an account, not merely a session.** On a console with no accounts
        every request is anonymous and allowed — but a watchlist with no owner is
        the shared list that accounts exist to replace, so there is nothing here for
        that visitor to edit and saying so is better than inventing an owner.

        Opened read-write for the duration, rather than through
        `console.read_session`. The single-writer FILE lock that `tracker` commands
        take is deliberately *not* acquired: it is held for the hours a crawl runs,
        and a watchlist edit that fails because the nightly ingest is halfway
        through would be a worse answer than waiting. SQLite's `busy_timeout` (5s,
        set in `db._apply_pragmas`) is the right instrument for a single-row insert,
        and a genuine contention still surfaces as an error rather than a silent
        no-op.
        """
        if not self.console.allow_watch:
            return self._error(403, "this console was started with --no-watch-edits")

        account_id = self._account_id
        if account_id is None:
            return self._error(
                403,
                "a watchlist belongs to an account, and this console has none. "
                "Create one with `tracker users add` and sign in.",
            )

        from tracker.watchlist import WatchError, add, remove

        action = str(body.get("action") or "").strip()
        entry = str(body.get("entry") or "").strip()
        note = body.get("note")
        if action not in {"add", "remove"}:
            return self._error(400, "action must be 'add' or 'remove'")
        if not entry:
            return self._error(400, "entry is required")
        if len(entry) > 200:
            return self._error(400, "entry is too long")
        if note is not None and not isinstance(note, str):
            return self._error(400, "note must be a string")

        engine = open_db(self.console.db_path, readonly=False)
        try:
            with session_scope(engine) as session:
                if action == "add":
                    row, created = add(session, entry, account_id=account_id, note=(note or None))
                    result = {"entry": row.entry, "created": created}
                else:
                    result = {
                        "entry": entry,
                        "removed": remove(session, entry, account_id=account_id),
                    }
                # Read back inside the same session, so the response describes the
                # list as this write left it rather than as a second connection
                # happened to find it.
                result["watchlist"] = self._watchlist_json(session, account_id)
        except WatchError as exc:
            return self._error(400, str(exc))
        except OperationalError as exc:
            # "database is locked": something else is writing. Actionable, and not
            # a 500 — the request was fine, the moment was not.
            if "locked" not in str(exc).lower():
                raise
            return self._error(503, "the database is busy writing; try again in a moment")

        self._json(result)

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
            from tracker.llm import LLMUnavailable, reasoning_extractor

            try:
                extractor = reasoning_extractor(get_settings())
            except LLMUnavailable as exc:
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
            from tracker.llm import LLMUnavailable, fast_extractor

            try:
                extractor = fast_extractor(get_settings())
            except LLMUnavailable as exc:
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
            from tracker.llm import LLMUnavailable, fast_extractor

            try:
                extractor = fast_extractor(get_settings())
            except LLMUnavailable as exc:
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
            from tracker.llm import LLMUnavailable, fast_extractor

            try:
                extractor = fast_extractor(get_settings())
            except LLMUnavailable as exc:
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


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def serve(
    db_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    allow_ai: bool = True,
    allow_watch: bool = True,
) -> None:
    """Run the console until interrupted."""
    console = Console(db_path, allow_ai=allow_ai, allow_watch=allow_watch)
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
