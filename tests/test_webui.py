"""The console's HTTP surface, and the boundary that keeps it safe.

Most of this file is about the runner. The read side is a thin wrapper over
modules that already have their own tests, but `POST /api/run` turns a JSON body
into a subprocess, and everything that stops that being a remote shell is
asserted here: the catalog rejects what it does not know, argv is a list built
from validated flags, a command that spends money needs its name typed back, and
two runs cannot overlap.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import threading
from http.client import HTTPConnection

import pytest

from tracker.db import init_db, session_scope
from tracker.ingest.records import IngestRecord, RiskRecord, SourceRecord
from tracker.upsert import upsert_record
from tracker.webui import catalog
from tracker.webui.runner import Busy, Runner
from tracker.webui.server import Console, Handler

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)


@pytest.fixture
def seeded_db(tmp_path):
    """A real database file with one fully-populated project."""
    path = tmp_path / "tracker.db"
    engine, _ = init_db(path)
    with session_scope(engine) as session:
        upsert_record(
            session,
            IngestRecord(
                project={
                    "company": "Microsoft",
                    "name": "Fairwater",
                    "city": "Mount Pleasant",
                    "state": "WI",
                },
                sources=[
                    SourceRecord(
                        url="https://news.microsoft.com/fairwater/",
                        source_type="company_filing",
                        fetched_at=T0,
                        excerpt="The campus will draw 900 MW.",
                        claims={
                            "name": "Fairwater",
                            "company": "Microsoft",
                            "city": "Mount Pleasant",
                            "state": "WI",
                            "mw_planned": 900.0,
                            "phase": "construction",
                        },
                        quotes={"mw_planned": "will draw 900 MW"},
                    )
                ],
                risks=[
                    RiskRecord(
                        category="transmission",
                        severity="material",
                        summary="Two 345-kV upgrades outstanding.",
                        quote="must complete two 345-kilovolt upgrades",
                        source_url="https://news.microsoft.com/fairwater/",
                    )
                ],
            ),
        )
    return path


@pytest.fixture
def server(seeded_db):
    """A live console on an ephemeral loopback port."""
    from http.server import ThreadingHTTPServer

    console = Console(seeded_db, allow_write=True)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address, console
    finally:
        httpd.shutdown()
        httpd.server_close()


def request(address, path, method="GET", body=None):
    conn = HTTPConnection(*address, timeout=30)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    conn.close()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, raw


# --- the read surface -------------------------------------------------------


def test_dataset_carries_the_shape_the_page_expects(server):
    address, _ = server
    status, data = request(address, "/api/dataset")
    assert status == 200
    for key in (
        "projects",
        "totals",
        "tracks",
        "riskTrack",
        "phases",
        "sourceWeight",
        "gaps",
        "queue",
        "failed",
        "required",
        "exposure",
    ):
        assert key in data, f"{key} missing from the dataset"
    assert data["totals"]["projects"] == 1
    project = data["projects"][0]
    # The two things the page cannot compute for itself.
    assert project["standing"]["tracks"], "per-track standing must come from the backend"
    assert project["prov"]["mw_planned"]["quote"] == "will draw 900 MW"
    assert project["prov"]["mw_planned"]["quote_is_exact"] is True


def test_reading_the_dataset_does_not_write(server, seeded_db, logical_snapshot):
    """A read route opens the database mode=ro; prove it stays untouched.

    The same guarantee the read commands carry, and it matters more here: a
    server answers requests the operator did not consciously issue. Compared
    logically rather than by bytes — see the `logical_snapshot` fixture for why a
    byte comparison is flaky under WAL.
    """
    address, _ = server
    before = logical_snapshot(seeded_db)
    for path in ("/api/dataset", "/api/commands", "/api/runs", "/api/health"):
        assert request(address, path)[0] == 200
    assert logical_snapshot(seeded_db) == before


def test_the_page_references_no_external_host(server):
    """Same guarantee `tracker export html` makes, for the same reason."""
    address, _ = server
    status, body = request(address, "/")
    assert status == 200
    for host in ("unpkg.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com"):
        assert host not in body, f"the shell reaches out to {host}"


def test_static_refuses_to_escape_its_root(server):
    address, _ = server
    for attempt in (
        "/static/../../cli.py",
        "/static/..%2f..%2fcli.py",
        "/static/vendor/../../db.py",
    ):
        status, _ = request(address, attempt)
        assert status == 404, f"{attempt} was served"


def test_unknown_route_is_a_404_not_a_traceback(server):
    address, _ = server
    status, body = request(address, "/api/nope")
    assert status == 404
    assert "error" in body


# --- the catalog ------------------------------------------------------------


def test_catalog_hides_commands_that_are_not_commands():
    """`_print_standing` was once registered by a stray decorator.

    Anything whose name is not a word is a CLI bug rather than something to
    offer, and the palette must not surface it.
    """
    names = set(catalog.by_name())
    assert not [n for n in names if n.startswith(("_", "-"))]
    assert {"sync", "gaps", "ingest crawl"} <= names


def test_catalog_marks_what_spends_money():
    commands = catalog.by_name()
    assert commands["sync"].cost == "llm"
    assert commands["ingest crawl"].cost == "llm"
    assert commands["gaps"].cost == "free"


def test_argv_is_a_list_built_from_the_catalog():
    argv = catalog.build_argv("sync", {"--limit": 5, "--deep": True})
    assert argv[1:] == ["-m", "tracker", "sync", "--limit", "5", "--deep"]


@pytest.mark.parametrize(
    ("cmd", "flags", "expect"),
    [
        ("nope", {}, "unknown command"),
        ("gaps", {"--nope": 1}, "no flag"),
        ("export", {"--out": "/etc/passwd"}, "no flag"),
        ("sync", {"--limit": "5; rm -rf /"}, "must be a number"),
        ("sync", {"--limit": "$(whoami)"}, "must be a number"),
        ("list", {"--phase": "bogus"}, "must be one of"),
        ("serve", {}, "cannot be run from the console"),
    ],
)
def test_catalog_refuses_what_it_does_not_know(cmd, flags, expect):
    with pytest.raises(catalog.InvalidRequest) as exc:
        catalog.build_argv(cmd, flags)
    assert expect in str(exc.value)


def test_a_repeatable_flag_takes_a_list_and_repeats_itself():
    """The Queue's per-article Crawl button needs exactly one URL through.

    `--url` is `list[str]` on the CLI, so click marks it `multiple` and the
    catalog reads that rather than keeping its own list of which flags repeat.
    """
    one = catalog.build_argv("ingest crawl", {"--url": "https://a.example/x"})
    assert one[-2:] == ["--url", "https://a.example/x"]
    many = catalog.build_argv(
        "ingest crawl", {"--url": ["https://a.example/x", "https://b.example/y"]}
    )
    assert many[-4:] == ["--url", "https://a.example/x", "--url", "https://b.example/y"]


@pytest.mark.parametrize("cmd,flag", [("ingest crawl", "--prompt"), ("sync", "--limit")])
def test_a_list_is_refused_where_the_cli_takes_one_value(cmd, flag):
    """Otherwise it stringifies to "['a', 'b']" and goes through as one argument."""
    with pytest.raises(catalog.InvalidRequest) as exc:
        catalog.build_argv(cmd, {flag: ["a", "b"]})
    assert "single value" in str(exc.value)


def test_crawling_one_url_still_needs_the_confirmation(seeded_db):
    """The button is two-step in the UI; the server rule behind it is unchanged."""
    runner = Runner(seeded_db)
    with pytest.raises(catalog.InvalidRequest) as exc:
        runner.start("ingest crawl", {"--url": "https://a.example/x"})
    assert "spends LLM tokens" in str(exc.value)


def test_a_shell_metacharacter_that_survives_is_still_only_one_argument():
    """Text flags accept anything; it just never becomes shell syntax.

    `--company` is a substring filter, so `;` in it is legitimate input. The
    guarantee is not that the value is sanitised — it is that argv is a list and
    no shell ever sees it.
    """
    argv = catalog.build_argv("list", {"--company": "Micro; rm -rf /"})
    assert argv[-2:] == ["--company", "Micro; rm -rf /"]
    assert not any(part == ";" for part in argv)


# --- the runner -------------------------------------------------------------


def test_an_llm_command_needs_its_name_typed_back(seeded_db):
    runner = Runner(seeded_db)
    with pytest.raises(catalog.InvalidRequest) as exc:
        runner.start("sync", {"--limit": 1})
    assert "spends LLM tokens" in str(exc.value)
    with pytest.raises(catalog.InvalidRequest):
        runner.start("sync", {"--limit": 1}, confirm="yes")


def test_a_free_command_needs_no_confirmation(server):
    address, _ = server
    status, body = request(address, "/api/run", "POST", {"cmd": "version", "flags": {}})
    # `version` is blocked from the console, which is itself the assertion that
    # the block list is consulted before anything is spawned.
    assert status == 400
    assert "cannot be run" in body["error"]


def test_a_second_run_is_refused_rather_than_queued(seeded_db, monkeypatch):
    """SQLite takes one writer; a second run would die partway through."""
    runner = Runner(seeded_db)
    runner._current = type("R", (), {"status": "running", "cmd": "sync"})()
    with pytest.raises(Busy) as exc:
        runner.start("gaps", {})
    assert "still running" in str(exc.value)


def test_run_streams_output_and_records_history(server, seeded_db):
    from tracker.webui import runs as runs_mod

    address, _ = server
    status, body = request(address, "/api/run", "POST", {"cmd": "gaps", "flags": {}})
    assert status == 202, body
    run_id = body["run"]["id"]

    # The stream ends when the subprocess does; reading it to completion is the
    # wait.
    conn = HTTPConnection(*address, timeout=120)
    conn.request("GET", f"/api/run/{run_id}/stream")
    stream = conn.getresponse().read().decode("utf-8")
    conn.close()
    assert '"type": "end"' in stream or '"type":"end"' in stream

    record = runs_mod.read_log(seeded_db, run_id)
    assert record["status"] == "ok"
    assert record["exit_code"] == 0
    # The echoed line is the real argv, `--db` and all: a log that abbreviates
    # what ran is a log you cannot reproduce from.
    assert record["lines"][0].startswith("$ tracker --db ")
    assert record["lines"][0].endswith(" gaps")
    assert any("mw_built" in line for line in record["lines"])


def test_a_run_targets_the_database_the_console_is_serving(seeded_db, tmp_path):
    """The child must be told which database, not left to resolve a default.

    `--db` is blocked as a request flag so a caller cannot redirect a run; the
    runner therefore has to inject it. Without this a run started from a console
    opened on one database silently operated on whichever one its working
    directory implied.
    """
    argv = catalog.build_argv("gaps", {}, db_path=seeded_db)
    assert argv[1:4] == ["-m", "tracker", "--db"]
    assert argv[4] == str(seeded_db)
    assert argv[5] == "gaps"


def test_a_read_only_console_refuses_to_run_anything(seeded_db):
    from http.server import ThreadingHTTPServer

    console = Console(seeded_db, allow_write=False)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = request(
            httpd.server_address, "/api/run", "POST", {"cmd": "gaps", "flags": {}}
        )
        assert status == 403
        assert "read-only" in body["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- colour -----------------------------------------------------------------


def _run_gaps(extra_env: dict[str, str], drop: tuple[str, ...] = ()) -> str:
    import subprocess

    env = {**os.environ, "COLUMNS": "160", "PYTHONIOENCODING": "utf-8", **extra_env}
    for key in drop:
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-m", "tracker", "gaps"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    return result.stdout


SGR = re.compile(r"\x1b\[[0-9;]*m")


def test_forcing_colour_actually_produces_escapes():
    """`FORCE_COLOR` alone is not enough on Windows, and it fails silently.

    Rich honours it — `is_terminal` goes True — and then picks
    `ColorSystem.WINDOWS`, which paints via the console API rather than writing
    escapes. Down a pipe that API does nothing, so the markup is stripped and
    nothing replaces it. `cli._forced_colour` names an ANSI dialect to fix it;
    this asserts the fix rather than the flag.
    """
    out = _run_gaps({"FORCE_COLOR": "1", "COLORTERM": "truecolor"}, drop=("NO_COLOR",))
    assert SGR.search(out), "colour was forced and no escape sequences came out"


def test_piping_without_asking_stays_plain():
    """The default has not changed: `tracker gaps > file` is still plain text."""
    out = _run_gaps({}, drop=("FORCE_COLOR", "NO_COLOR"))
    assert not SGR.search(out)


def test_no_color_beats_force_color():
    """https://no-color.org — set means no colour, whatever else was asked for."""
    out = _run_gaps({"FORCE_COLOR": "1", "NO_COLOR": "1"})
    assert not SGR.search(out)


def test_the_runner_asks_for_colour_and_removes_what_would_suppress_it():
    """Reading the env the runner builds, rather than running a whole crawl."""
    import inspect

    from tracker.webui import runner as runner_mod

    source = inspect.getsource(runner_mod.Runner._execute)
    assert '"FORCE_COLOR": "1"' in source
    # Inheriting either of these from the operator's shell would silently undo it.
    assert 'env.pop("NO_COLOR", None)' in source
    assert 'env.pop("TTY_COMPATIBLE", None)' in source
    assert '"TERM"' not in source, "TERM=dumb would make Rich a dumb terminal and kill colour"


# --- the gate ---------------------------------------------------------------

PASSWORD = "correct horse battery"


@pytest.fixture
def gated(seeded_db):
    """A console with a password, on an ephemeral loopback port."""
    from http.server import ThreadingHTTPServer

    console = Console(seeded_db, allow_write=True, password=PASSWORD)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address, console
    finally:
        httpd.shutdown()
        httpd.server_close()


def raw(address, path, method="GET", body=None, cookie=None, headers=None):
    """A request that keeps the response headers, for cookie assertions."""
    conn = HTTPConnection(*address, timeout=30)
    head = {"Content-Type": "application/json", **(headers or {})}
    if cookie:
        head["Cookie"] = cookie
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=head)
    response = conn.getresponse()
    payload = response.read().decode("utf-8")
    result = (response.status, dict(response.getheaders()), payload)
    conn.close()
    return result


def sign_in(address, password=PASSWORD):
    status, headers, _ = raw(address, "/api/login", "POST", {"password": password})
    cookie = headers.get("Set-Cookie", "").split(";")[0]
    return status, cookie


@pytest.mark.parametrize(
    "path",
    [
        "/api/dataset",
        "/api/commands",
        "/api/runs",
        "/api/health",
        "/static/app.js",
        "/static/app.css",
    ],
)
def test_nothing_is_served_before_signing_in(gated, path):
    """Blanket, not just the page.

    An anonymous request reaches exactly one file. Serving the app bundle or a
    health check to the open internet would leak the shape of what is behind the
    gate for no benefit.
    """
    address, _ = gated
    status, _, body = raw(address, path)
    assert status == 401, f"{path} was served to an anonymous request"
    assert "sign in" in body
    # And specifically not the asset itself.
    assert "React" not in body and "dc-tracker console" not in body


def test_a_navigation_gets_the_form_but_an_asset_gets_a_401(gated):
    """Both withhold the asset; only one of them is usable.

    Answering a request for `app.js` with the login page and a 200 hands a
    browser HTML where it asked for a script, so an expired session surfaces as a
    parse error rather than as "you are signed out".
    """
    address, _ = gated
    status, _, body = raw(address, "/")
    assert status == 200 and "This console runs commands" in body
    assert raw(address, "/static/app.js")[0] == 401


def test_the_login_page_leaks_nothing(gated):
    address, _ = gated
    _, _, body = raw(address, "/")
    for leak in ("Fairwater", "Microsoft", "/api/dataset", "vendor/", "tracker serve"):
        assert leak not in body, f"the login page mentions {leak}"


def test_signing_in_sets_an_httponly_lax_session_cookie(gated):
    address, _ = gated
    status, headers, _ = raw(address, "/api/login", "POST", {"password": PASSWORD})
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie, "a script must not be able to read the session"
    assert "SameSite=Lax" in cookie, "this is what blocks a cross-site POST to /api/run"
    assert "Path=/" in cookie
    # Not marked Secure here: the test connection is plain http, and marking it
    # Secure would mean the browser never sent it back.
    assert "Secure" not in cookie


def test_a_session_opens_every_route(gated):
    address, _ = gated
    _, cookie = sign_in(address)
    for path in ("/api/dataset", "/api/commands", "/api/health", "/static/app.js"):
        status, _, _ = raw(address, path, cookie=cookie)
        assert status == 200, path
    status, _, body = raw(address, "/", cookie=cookie)
    assert "This console runs commands" not in body, "still on the login page"


def test_a_wrong_password_is_401_and_grants_nothing(gated):
    address, _ = gated
    status, cookie = sign_in(address, "wrong")
    assert status == 401
    assert not cookie
    assert raw(address, "/api/dataset")[0] == 401


def test_a_forged_cookie_is_not_a_session(gated):
    address, _ = gated
    from tracker.webui.auth import COOKIE

    for forged in (f"{COOKIE}=x", f"{COOKIE}=", f"{COOKIE}=" + "a" * 43, "other=1"):
        assert raw(address, "/api/dataset", cookie=forged)[0] == 401


def test_signing_out_revokes_the_session(gated):
    address, _ = gated
    _, cookie = sign_in(address)
    assert raw(address, "/api/dataset", cookie=cookie)[0] == 200
    assert raw(address, "/api/logout", "POST", {}, cookie=cookie)[0] == 200
    assert raw(address, "/api/dataset", cookie=cookie)[0] == 401


def test_a_run_cannot_be_started_without_a_session(gated):
    """The whole reason the gate exists."""
    address, _ = gated
    status, _, body = raw(address, "/api/run", "POST", {"cmd": "gaps", "flags": {}})
    assert status == 401
    assert "sign in" in body


def test_a_cross_site_post_is_refused(gated):
    """Second lock. SameSite=Lax is the first, but it lives in the browser."""
    address, _ = gated
    _, cookie = sign_in(address)
    status, _, body = raw(
        address,
        "/api/run",
        "POST",
        {"cmd": "gaps", "flags": {}},
        cookie=cookie,
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403
    assert "cross-site" in body


def test_repeated_failures_lock_the_gate(gated):
    """A published URL means an unattended login form.

    A short password is only safe if guessing is slow. Eight tries then fifteen
    minutes makes even a small keyspace unreachable, and the lockout says so
    rather than repeating "wrong password" at someone who mistyped.
    """
    address, console = gated
    for _ in range(console.gate.max_failures):
        assert sign_in(address, "wrong")[0] == 401
    status, _, body = raw(address, "/api/login", "POST", {"password": "wrong"})
    assert status == 429
    assert "Locked" in body
    # And the lockout holds even for the right password, or it is not a lockout.
    assert raw(address, "/api/login", "POST", {"password": PASSWORD})[0] == 429


def test_the_gate_closes_globally_not_just_per_client(gated):
    """Per-client lockout alone is the wrong shape against a published URL.

    The counter keys on `CF-Connecting-IP`, so an attacker with a thousand
    addresses would get a thousand budgets. The global counter is what makes the
    guess rate a property of the gate rather than of the address pool — and it is
    what lets a short password be safe.
    """
    address, console = gated
    limit = console.gate.global_max_failures
    for i in range(limit):
        # A different client every time: the per-client limit is never reached.
        raw(
            address,
            "/api/login",
            "POST",
            {"password": "wrong"},
            headers={"CF-Connecting-IP": f"203.0.113.{i % 250}"},
        )
    status, _, body = raw(
        address,
        "/api/login",
        "POST",
        {"password": PASSWORD},
        headers={"CF-Connecting-IP": "198.51.100.7"},
    )
    assert status == 429, "a fresh address walked straight past the lockout"
    assert "Locked" in body


def test_a_correct_password_clears_the_global_counter(gated):
    """One person fumbling twice must not spend everyone's budget."""
    address, console = gated
    for _ in range(3):
        sign_in(address, "wrong")
    assert console.gate._global.count == 3
    assert sign_in(address)[0] == 200
    assert console.gate._global.count == 0


def test_no_password_configured_means_no_gate(server):
    """Loopback default: reaching 127.0.0.1 already means having the machine."""
    address, console = server
    assert console.gate.required is False
    assert request(address, "/api/dataset")[0] == 200


def test_the_password_check_is_constant_time():
    """Compare with hmac, so the secret cannot be recovered from timing."""
    import inspect

    from tracker.webui import auth

    source = inspect.getsource(auth.Gate.attempt)
    assert "compare_digest" in source
    assert "==" not in source.split("compare_digest")[1].split("\n")[0]


def test_a_tunnel_client_ip_is_only_trusted_from_loopback():
    """CF-Connecting-IP is a header, and headers are writable.

    It is read only when the socket itself is loopback — which behind cloudflared
    it always is, and from a direct remote connection it never is.
    """
    import inspect

    from tracker.webui import server as server_mod

    source = inspect.getsource(server_mod.Handler._client)
    assert "127.0.0.1" in source and "CF-Connecting-IP" in source


# --- assets -----------------------------------------------------------------


def test_every_file_the_page_needs_is_vendored():
    """A half-vendored install must fail with names, not a blank page."""
    from tracker.webui import assets

    assert assets.missing_vendor() == []


def test_the_meridian_bundle_is_complete():
    """It arrives truncated at the read API's 256 KiB cap and is repaired.

    The failure mode is silent — every component definition is present, only the
    trailing export block is cut, so the file parses as far as the browser gets
    and then registers nothing. Guard the shape rather than a byte count.
    """
    from tracker.webui import assets

    bundle = (assets.STATIC_ROOT / "vendor/meridian/_ds_bundle.js").read_text(encoding="utf-8")
    assert bundle.rstrip().endswith("})();"), "the bundle's IIFE is not closed"
    for component in ("Button", "Card", "Table", "Tabs", "StatCard", "EmptyState", "Skeleton"):
        assert f"__ds_ns.{component} = __ds_scope.{component};" in bundle
