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

    def __init__(self, db_path: Path, *, allow_write: bool = True) -> None:
        self.db_path = db_path
        self.allow_write = allow_write
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

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

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

    # --- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's naming contract
        parsed = urlsplit(self.path)
        route, query = parsed.path, parse_qs(parsed.query)
        try:
            self._route_get(route, query)
        except MigrationError as exc:
            self._error(503, str(exc))
        except FileNotFoundError as exc:
            self._error(503, str(exc))
        except BrokenPipeError:
            pass  # the tab closed mid-response
        except Exception:
            log.exception("GET %s failed", route)
            self._error(500, "internal error; see the server log")

    do_HEAD = do_GET

    def _route_get(self, route: str, query: dict[str, list[str]]) -> None:
        if route in {"/", "/index.html"}:
            return self._page()
        if route.startswith("/static/"):
            return self._static(route[len("/static/") :])
        if route == "/api/dataset":
            return self._dataset()
        if route == "/api/commands":
            return self._json(
                {"groups": catalog.grouped_json(), "llm": sorted(catalog.LLM_COMMANDS)}
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
            if parsed.path == "/api/run":
                return self._start_run(body)
            if parsed.path == "/api/run/cancel":
                return self._json({"cancelled": self.console.runner.cancel()})
            self._error(404, f"no route {parsed.path!r}")
        except BrokenPipeError:
            pass
        except Exception:
            log.exception("POST %s failed", parsed.path)
            self._error(500, "internal error; see the server log")

    # --- handlers ---------------------------------------------------------

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
        except (BrokenPipeError, ConnectionResetError):
            pass
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
) -> None:
    """Run the console until interrupted."""
    console = Console(db_path, allow_write=allow_write)
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
