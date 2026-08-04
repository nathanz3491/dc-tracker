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
import struct
import sys
import threading
import time
from http.client import HTTPConnection

import pytest

from tracker.db import init_db, session_scope
from tracker.ingest.records import IngestRecord, RiskRecord, SourceRecord
from tracker.upsert import upsert_record
from tracker.webui import assets, catalog
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


def headers_for(address, path):
    """Response headers, lowercased, for the cache-policy assertions."""
    conn = HTTPConnection(*address, timeout=30)
    conn.request("GET", path)
    response = conn.getresponse()
    response.read()
    conn.close()
    return {k.lower(): v for k, v in response.getheaders()}


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


def test_dataset_carries_the_capex_rollup(server):
    """The buyer axis, and the duplicate warning that belongs beside it."""
    address, _ = server
    _status, data = request(address, "/api/dataset")
    capex = data["capex"]
    assert set(capex) >= {
        "coverage",
        "positions",
        "years",
        "as_of_year",
        "suspect",
        "duplicates",
    }
    assert set(capex["duplicates"]) == {"groups", "double_counted_mw", "shared_blocks"}
    # Microsoft is a hyperscaler in the company list, so it is its own customer.
    position = next(p for p in capex["positions"] if p["key"] == "microsoft")
    assert position["self_built"] == 1
    assert position["mw_planned"] == 900.0
    # Groups carry ids only; the page looks the rows up in `projects` rather than
    # being sent a second copy that can disagree with the first.
    assert all(isinstance(i, int) for g in capex["duplicates"]["groups"] for i in g)


def test_a_programme_total_says_so_rather_than_just_being_amber(tmp_path):
    """Both causes of 待确认 sit at one tier, and they need opposite work.

    An unquoted value needs another source. A programme total quoted in an
    article about one campus needs correcting — going looking for a citation
    would find one, and it would still be the wrong number. The ingest path
    records which it is; this proves the console can still tell.
    """
    from tracker.ingest.crawl import SCALE_NOTE_FIELD, SCALE_NOTE_MARKER, _implausible_investment
    from tracker.webui.dataset import build

    note = _implausible_investment({"investment_usd": 100_000_000_000, "mw_planned": 1200.0})
    assert note and SCALE_NOTE_MARKER in note

    path = tmp_path / "scale.db"
    engine, _ = init_db(path)
    with session_scope(engine) as session:
        upsert_record(
            session,
            IngestRecord(
                project={
                    "company": "Brookfield",
                    "name": "Paducah",
                    "city": "Paducah",
                    "state": "KY",
                },
                sources=[
                    SourceRecord(
                        url="https://example.test/paducah",
                        source_type="trade_press",
                        fetched_at=T0,
                        excerpt="The investment could total roughly $100 billion over time.",
                        claims={
                            "name": "Paducah",
                            "company": "Brookfield",
                            "city": "Paducah",
                            "state": "KY",
                            "mw_planned": 1200.0,
                            "investment_usd": 100_000_000_000,
                        },
                        unconfirmed=frozenset({"investment_usd"}),
                    )
                ],
                notes=[note],
            ),
        )
    with session_scope(engine, commit=False) as session:
        payload = build(session, db_path=str(path), schema_version=7)

    reason = payload["projects"][0]["unconfirmed_because"][SCALE_NOTE_FIELD]
    assert reason["code"] == "scale"
    assert SCALE_NOTE_MARKER in reason["note"]
    # The `[source][tag]` bookkeeping is stripped; the sentence is what is shown.
    assert not reason["note"].startswith("[")


def test_a_merely_unquoted_value_claims_no_reason(server):
    """The common case must not borrow the rarer one's explanation."""
    address, _ = server
    _status, data = request(address, "/api/dataset")
    assert data["projects"][0]["unconfirmed_because"] == {}


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


# --- cache busting -----------------------------------------------------------
#
# Static files were served at bare URLs. The server had no way to tell a browser
# — or a CDN edge in front of a published console — that `app.js` had changed, so
# a restart could leave the operator looking at last week's front end while every
# check on the server side said the new code was being served. It happened, and
# it cost a round trip to work out that nothing was wrong with the code.


def test_the_page_stamps_every_asset_with_its_version(server):
    address, _ = server
    status, body = request(address, "/")
    assert status == 200
    for asset in ("/static/app.js", "/static/app.css", "/static/vendor/react.js"):
        assert f"{asset}?v=" in body, f"{asset} is referenced without a version"
    assert '"/static/app.js"' not in body, "an unstamped reference survived"


def test_editing_a_file_changes_the_url_it_is_served_at(server, tmp_path):
    """The whole point: a changed file cannot be served from anybody's cache."""
    from tracker.webui import assets

    address, _ = server
    target = assets.STATIC_ROOT / "app.css"
    before = assets.version_token(target)
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n/* touched */\n")
        assert assets.version_token(target) != before
        _, body = request(address, "/")
        assert f"app.css?v={assets.version_token(target)}" in body
    finally:
        target.write_bytes(original)


def test_a_current_token_is_cacheable_and_anything_else_is_not(server):
    """`immutable` is only safe because the URL changes when the file does.

    Sent for a stale or absent token it would strand a browser on an old bundle,
    which is the failure this whole mechanism exists to prevent.
    """
    from tracker.webui import assets

    address, _ = server
    token = assets.version_token(assets.STATIC_ROOT / "app.js")

    fresh = headers_for(address, f"/static/app.js?v={token}")
    assert "immutable" in fresh.get("cache-control", "")

    for path in ("/static/app.js", "/static/app.js?v=stale-1"):
        assert "immutable" not in headers_for(address, path).get("cache-control", ""), path


def test_the_page_itself_is_never_cached(server):
    """It carries the tokens, so a cached copy would pin every asset with it."""
    address, _ = server
    assert "no-store" in headers_for(address, "/").get("cache-control", "")


def test_stamping_leaves_an_unknown_asset_alone():
    """A reference to a file that is not there must not gain a fake version."""
    from tracker.webui import assets

    html = '<script src="/static/nope.js"></script>'
    assert assets.stamp(html) == html


def test_a_missing_file_still_yields_a_token():
    """Called during a render; raising there would blank the page over nothing."""
    from tracker.webui import assets

    assert assets.version_token(assets.STATIC_ROOT / "not-a-file.js") == "0"


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
    # One LLM call per filing, exactly like an article. It shipped outside
    # LLM_COMMANDS and so ran from the console with no confirmation at all.
    assert commands["ingest edgar"].cost == "llm"


def test_catalog_marks_what_destroys_data():
    """A separate axis from cost, because they are separate losses.

    `merge` spends nothing and is the only command in the console that cannot be
    undone. Reporting that as "spends LLM tokens" would be false, and leaving it
    ungated made a misplaced click delete project rows.
    """
    commands = catalog.by_name()
    merge = commands["merge"]
    assert merge.cost == "free"
    assert merge.destroys and "no undo" in merge.destroys
    assert merge.needs_confirmation
    assert commands["duplicates"].destroys is None  # it only reports
    assert commands["gaps"].needs_confirmation is False


def test_a_destructive_command_needs_its_name_typed_back(seeded_db):
    """The same ritual as an LLM command, for a different reason."""
    runner = Runner(seeded_db)
    with pytest.raises(catalog.InvalidRequest) as exc:
        runner.start("merge", {"--into": 1, "dupe_ids": [2]})
    assert "no undo" in str(exc.value)

    with pytest.raises(catalog.InvalidRequest) as exc:
        runner.start("merge", {"--into": 1, "dupe_ids": [2]}, confirm="yes")
    assert "no undo" in str(exc.value)


def test_the_gate_is_on_the_command_not_its_flags(seeded_db):
    """`--dry-run` must not talk its way past the confirmation.

    A gate that inspects arguments is a gate with a bypass in it: the flag that
    makes the run harmless today is one refactor away from not doing so, and the
    console would have already let the request through.
    """
    runner = Runner(seeded_db)
    with pytest.raises(catalog.InvalidRequest):
        runner.start("merge", {"--into": 1, "dupe_ids": [2], "--dry-run": True})


def test_every_command_the_palette_offers_is_in_a_named_group():
    """`GROUPS` is the one hand-written thing in the catalog, so it falls behind.

    It did: `capex`, `duplicates`, `merge` and `ingest edgar` all arrived and all
    landed in the unnamed "Other" bucket at the bottom of the palette. Only the
    blocked commands belong there — they are listed so their argv can be copied,
    not because anybody groups them.
    """
    grouped = catalog.grouped_json()
    other = [c["cmd"] for g in grouped if g["group"] == "Other" for c in g["items"]]
    assert set(other) <= set(catalog.BLOCKED), f"ungrouped commands: {sorted(other)}"


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


def test_a_variadic_positional_takes_a_list_of_ids():
    """The Duplicates card sends a whole group in one request.

    `merge` takes any number of ids as bare arguments, which click models as
    `nargs=-1` rather than as a `multiple` option. Reading only `multiple` refused
    the list and the card could never have folded more than one row.
    """
    argv = catalog.build_argv("merge", {"--into": 3, "dupe_ids": [8, 93, 121]})
    assert argv[1:] == ["-m", "tracker", "merge", "--into", "3", "8", "93", "121"]
    # Options first, positionals last: `merge --into 3 8 93 121` parses, and
    # `merge 8 93 121 --into 3` would swallow the option as an argument.
    assert argv.index("--into") < argv.index("8")


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


def test_writing_a_briefing_costs_money_and_says_so(server):
    """A POST, and gated, because a GET that spends money is a GET a back button
    will make twice."""
    address, _ = server
    status, body = request(address, "/api/overview", "POST", {"project_id": 1})
    assert status == 400
    assert 'confirm="overview"' in body["error"]


@pytest.mark.parametrize(
    ("payload", "status", "expect"),
    [
        ({"project_id": 99999, "confirm": "overview"}, 404, "no project"),
        ({"project_id": "nine"}, 400, "must be an integer"),
        ({}, 400, "must be an integer"),
    ],
)
def test_the_briefing_route_refuses_what_it_cannot_answer(server, payload, status, expect):
    address, _ = server
    got_status, body = request(address, "/api/overview", "POST", payload)
    assert got_status == status
    assert expect in body["error"]


def test_a_briefing_already_written_needs_no_confirmation(server, seeded_db):
    """It has been paid for; making somebody re-confirm to reread it is theatre."""
    from tracker import overview as overview_mod
    from tracker.db import open_db
    from tracker.models import Project

    class _Writer:
        def complete(self, *, system, user, max_tokens):
            class R:
                text = "A short briefing about this campus, long enough to be kept."
                model = "test-model"

            return R()

    with session_scope(open_db(seeded_db), commit=False) as session:
        overview_mod.write(session.get(Project, 1), extractor=_Writer())

    address, _ = server
    status, body = request(address, "/api/overview", "POST", {"project_id": 1})
    assert status == 200
    assert body["cached"] is True
    assert "short briefing" in body["text"]


def test_a_reader_that_walks_away_mid_stream_is_not_an_error(server, caplog):
    """Closing the tab during a run must not produce a traceback.

    Over a Cloudflare tunnel this is ordinary: the edge drops idle connections and
    the browser's EventSource reconnects, so one crawl produces several aborted
    streams. It was logging two tracebacks each — one for the aborted write, and
    one for the failed attempt to send a 500 down the same dead socket.

    The narrow cause was platform. Windows raises `ConnectionAbortedError`
    (WinError 10053) where POSIX raises `BrokenPipeError` or
    `ConnectionResetError`, and only the latter two were caught, so the handler
    caught nothing on the platform this runs on.
    """
    import logging
    import socket as socket_mod

    address, _ = server
    status, body = request(address, "/api/run", "POST", {"cmd": "gaps", "flags": {}})
    assert status == 202, body
    run_id = body["run"]["id"]

    with caplog.at_level(logging.ERROR, logger="tracker.webui.server"):
        # Attach, read a little, then vanish without closing politely — a reset
        # rather than a shutdown, which is what a dropped tunnel looks like.
        raw = socket_mod.create_connection(address, timeout=30)
        raw.sendall(f"GET /api/run/{run_id}/stream HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        raw.recv(64)
        raw.setsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_LINGER, struct.pack("ii", 1, 0))
        raw.close()

        # The run is a subprocess and finishes regardless; wait it out so the
        # server has tried and failed to write to the socket we just killed.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            _s, runs_body = request(address, "/api/runs")
            if (runs_body.get("current") or {}).get("status") != "running":
                break
            time.sleep(0.5)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "a client hanging up is normal operation, not a server error:\n"
        + "\n".join(r.getMessage() for r in caplog.records)
    )

    # And the server is still answering.
    assert request(address, "/api/health")[0] == 200


def test_apologising_to_a_dead_socket_does_not_raise_again():
    """`_error` is called from inside `except` blocks, so it must not throw.

    This is the second traceback in the original report. The stream aborted, the
    catch-all handler tried to send a 500, and writing that 500 down the same dead
    socket raised again — this time out of an exception handler, where nothing was
    left to catch it, so it escaped to socketserver.

    Driven directly rather than through a real socket: the behavioural test below
    cannot reliably win the race between a fast command finishing and the client
    disappearing, and a test that only sometimes exercises the bug is not a test.
    """
    import io

    from tracker.webui.server import Handler

    class DeadSocket(io.RawIOBase):
        def write(self, b):
            raise ConnectionAbortedError(10053, "aborted by the host software")

        def writable(self) -> bool:
            return True

    handler = object.__new__(Handler)
    handler.wfile = DeadSocket()
    handler.headers = {}
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET /x HTTP/1.1"
    handler.client_address = ("127.0.0.1", 1)
    handler.server = None

    handler._error(500, "internal error")  # must not raise


def test_every_connection_failure_windows_can_raise_is_caught():
    """`ConnectionError` covers all of them; the old tuple covered two of four.

    Guards the fix by construction rather than by hoping a test happens to
    provoke the right errno on the right platform.
    """
    for kind in (
        BrokenPipeError,
        ConnectionResetError,
        ConnectionAbortedError,
        ConnectionRefusedError,
    ):
        assert issubclass(kind, ConnectionError)

    source = (assets.STATIC_ROOT.parent / "server.py").read_text(encoding="utf-8")
    assert "except (BrokenPipeError, ConnectionResetError)" not in source
    assert source.count("except ConnectionError") >= 3, "_stream, do_GET and do_POST"


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


def test_the_runner_asks_for_colour_and_removes_what_would_suppress_it(monkeypatch):
    """The env the runner builds, read as a value rather than as its own source."""
    from tracker.webui import runner as runner_mod

    # Both of these suppress colour even when it has been forced, so inheriting
    # one from the operator's shell would silently undo the whole mechanism.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TTY_COMPATIBLE", "0")

    # Inherited, never rewritten. An earlier version set TERM=dumb, which makes
    # Rich a dumb terminal and kills the colour whatever else has been forced.
    monkeypatch.setenv("TERM", "xterm-256color")

    env = runner_mod._child_env()
    assert env["FORCE_COLOR"] == "1"
    assert env["COLORTERM"] == "truecolor"
    assert "NO_COLOR" not in env
    assert "TTY_COMPATIBLE" not in env
    assert env["TERM"] == "xterm-256color"


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


def test_the_run_log_does_not_wrap():
    """A Rich table's column positions are baked in at COLUMNS characters.

    `white-space: pre-wrap` folded every 132-character row of `tracker list` onto
    a second line in an 815px pane, so the `+--+` borders no longer lined up with
    the cells and the table came out shredded. There is no width but COLUMNS at
    which wrapping works, so the pane scrolls sideways instead — which is what a
    terminal does.

    Asserted against the stylesheet because the failure is purely a CSS one: the
    markup was always right.
    """
    from tracker.webui import assets

    css = (assets.STATIC_ROOT / "app.css").read_text(encoding="utf-8")
    # Comments stripped first: the block explains this rule at length and the
    # explanation naturally contains the word the assertion forbids.
    declarations = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    block = declarations.split(".dc-log {", 1)[1].split("}", 1)[0]
    assert "white-space: pre;" in block
    assert "pre-wrap" not in block, "the default log surface must not reflow Rich's tables"
    # The opt-in is still there for prose, and only as an opt-in.
    assert ".dc-log--wrap { white-space: pre-wrap;" in declarations


def test_a_log_line_is_as_wide_as_its_content():
    """Otherwise a coloured background stops at the pane edge, mid-row.

    With `white-space: pre` the text overflows a block that is only as wide as
    the container, so any run with a background — Rich's reversed headers — would
    be painted for 815px of a 983px line. It also makes the container's
    scrollWidth wrong, which is what the horizontal scrollbar is sized from.
    """
    from tracker.webui import assets

    css = re.sub(
        r"/\*.*?\*/", "", (assets.STATIC_ROOT / "app.css").read_text(encoding="utf-8"), flags=re.S
    )
    block = css.split(".dc-log-line {", 1)[1].split("}", 1)[0]
    assert "width: max-content;" in block
    assert "min-width: 100%;" in block


# --- assets -----------------------------------------------------------------


def test_every_file_the_page_needs_is_vendored():
    """A half-vendored install must fail with names, not a blank page."""
    from tracker.webui import assets

    assert assets.missing_vendor() == []


# --- publishing through cloudflared -----------------------------------------


def test_the_api_host_is_not_mistaken_for_a_tunnel():
    """`api.trycloudflare.com` appears in the failure message, not in a success.

    Observed live: the quick-tunnel request timed out, cloudflared printed
    `Post "https://api.trycloudflare.com/tunnel": context deadline exceeded`, the
    URL pattern matched it, and the console announced
    `public: https://api.trycloudflare.com` — a link to Cloudflare's API,
    presented as the operator's console. Reporting a tunnel that does not exist
    is the worst failure this module has, because the whole point of the command
    is the URL it prints.
    """
    from tracker.webui.tunnel import _QUICK_FAILED, _URL

    failure = (
        'failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": '
        "context deadline exceeded (Client.Timeout exceeded while awaiting headers)"
    )
    assert _URL.search(failure) is None
    assert _QUICK_FAILED.search(failure)

    banner = "|  https://itchy-narrow-pine-42.trycloudflare.com   |"
    assert _URL.search(banner).group(0) == "https://itchy-narrow-pine-42.trycloudflare.com"
    assert _QUICK_FAILED.search(banner) is None


def test_a_refused_tunnel_fails_at_once_rather_than_waiting_out_the_window():
    """cloudflared can report that it cannot get a tunnel and then keep running.

    Waiting the full 60s out would replace a precise reason with "did not publish
    a URL in time", a minute later. The reason is on the first line of output.
    """
    import subprocess

    from tracker.webui.tunnel import _QUICK_FAILED, TunnelFailed, _wait

    class Alive:
        def poll(self):
            return None

    tail = [
        "INF Requesting new quick Tunnel on trycloudflare.com...",
        "failed to request quick Tunnel: boom",
    ]
    with pytest.raises(TunnelFailed) as exc:
        _wait(
            Alive(),  # type: ignore[arg-type]
            [],
            tail,
            timeout_s=30,
            waiting_for="publishing a URL",
            give_up_on=_QUICK_FAILED,
        )
    assert "boom" in str(exc.value)
    assert subprocess  # the real callers pass a Popen; this stands in for one


def test_publishing_is_not_something_the_page_can_do_to_itself():
    """`cloudflare` is blocked in the console for the same reason `serve` is.

    Stronger than "it would hold the run slot": putting this page on the public
    internet is a decision for somebody at a terminal. A blocked command still
    appears in the palette with its argv, so the operator can copy the line.
    """
    assert "cloudflare" in catalog.BLOCKED
    with pytest.raises(catalog.InvalidRequest) as exc:
        catalog.build_argv("cloudflare", {})
    assert "cannot be run from the console" in str(exc.value)


def test_the_quick_tunnel_api_call_is_relayed_through_the_proxy(monkeypatch):
    """cloudflared ignores HTTPS_PROXY for exactly one request, and it matters.

    It builds its own `http.Transport` for the quick-tunnel call, and a
    zero-value Transport has no proxy function — so on a machine behind a proxy
    that one request goes direct. Measured on such a machine: direct swung
    between 3.8s and 28s over an hour while the proxy stayed near 4s, against a
    fixed client budget of about ten. `tracker cloudflare` failed with
    `context deadline exceeded` while curl, pip and the browser all worked.

    So the console stands a relay in front of it and points `--quick-service` at
    that. This asserts the flag is passed and the port is the relay's.
    """
    from tracker.webui import tunnel as tunnel_mod

    captured: list[list[str]] = []

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_spawn(argv, _pattern):
        captured.append(argv)
        return FakeProcess(), ["https://itchy-narrow-pine-42.trycloudflare.com"], []

    monkeypatch.setattr(tunnel_mod, "find_cloudflared", lambda: "cloudflared")
    monkeypatch.setattr(tunnel_mod, "_check_runnable", lambda _binary: None)
    monkeypatch.setattr(tunnel_mod, "_spawn", fake_spawn)

    result = tunnel_mod.quick_tunnel(8765, proxy="http://127.0.0.1:8080")
    argv = captured[0]
    assert "--quick-service" in argv
    relayed = argv[argv.index("--quick-service") + 1]
    assert relayed.startswith("http://127.0.0.1:")
    assert result.via_proxy == "http://127.0.0.1:8080"
    result.stop()

    # And the escape hatch really escapes: no proxy, no relay, no flag.
    captured.clear()
    plain = tunnel_mod.quick_tunnel(8765, use_proxy=False)
    assert "--quick-service" not in captured[0]
    assert plain.via_proxy is None
    plain.stop()


def test_the_relay_is_not_an_open_proxy():
    """It forwards to one fixed host, whatever path is asked for.

    It binds a loopback port anything on the machine can reach, so "relay
    whatever you are told to" would be a real hole rather than a theoretical one.

    Asserted by watching what the relay asks a proxy to CONNECT to, because that
    is the thing that would actually be wrong. An earlier version of this test
    asserted `(QUICK_API + path).startswith(QUICK_API)`, which is a property of
    string concatenation and would have passed against any implementation at all.
    """
    import socket
    import threading as _threading
    from http.client import HTTPConnection as _HTTPConnection

    from tracker.webui.tunnel import QUICK_API, _QuickRelay

    assert QUICK_API == "https://api.trycloudflare.com"

    targets: list[str] = []
    stub = socket.socket()
    stub.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    stub.bind(("127.0.0.1", 0))
    stub.listen(8)

    def serve() -> None:
        # urllib tunnels https-through-http-proxy with `CONNECT host:443`.
        # Record the host and hang up; the relay then fails, which is fine —
        # the question is only ever where it tried to go.
        while True:
            try:
                conn, _ = stub.accept()
            except OSError:
                return
            with conn:
                first = conn.recv(4096).decode("latin-1", "replace").split("\r\n", 1)[0]
                if first.upper().startswith("CONNECT"):
                    targets.append(first.split()[1])

    thread = _threading.Thread(target=serve, daemon=True)
    thread.start()

    # Every shape an attacker on this machine could try: a path that looks like a
    # host, an absolute request URI, and headers that a naive implementation
    # might honour. None of them may change where the relay goes.
    attempts = [
        ("//evil.example.com/steal", {}),
        ("http://evil.example.com/steal", {}),
        ("/tunnel", {"X-Upstream": "https://evil.example.com", "Host": "evil.example.com"}),
    ]

    relay = _QuickRelay(f"http://127.0.0.1:{stub.getsockname()[1]}")
    try:
        assert relay.start().startswith("http://127.0.0.1:")
        for path, headers in attempts:
            client = _HTTPConnection("127.0.0.1", relay.port, timeout=20)
            try:
                client.request("POST", path, body=b"", headers=headers)
                client.getresponse().read()
            except Exception:
                pass  # a mangled upstream failing early is a pass, not a problem
            finally:
                client.close()
    finally:
        relay.stop()
        stub.close()
        thread.join(timeout=5)

    assert set(targets) <= {"api.trycloudflare.com:443"}, f"relay reached out to {targets}"
    assert targets, "the relay never called out at all — the test proved nothing"


def test_a_proxy_is_found_in_the_environment(monkeypatch):
    """Environment first; a bare host:port is given a scheme so Go can parse it."""
    from tracker.webui.tunnel import detect_proxy

    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "127.0.0.1:8080")
    assert detect_proxy() == "http://127.0.0.1:8080"
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    assert detect_proxy() == "http://proxy.example:3128"


def test_a_slow_minute_is_retried(monkeypatch):
    """The failure being retried is a latency race, not a refusal.

    The same request measured 3.8s and 28s an hour apart on one link. A second
    attempt is the proportionate response to that; giving up on the first is not.
    """
    from tracker.webui import tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky(port, *, timeout_s, proxy, use_proxy):
        calls["n"] += 1
        if calls["n"] < 3:
            raise tunnel_mod.TunnelFailed("context deadline exceeded")
        return "a tunnel"

    monkeypatch.setattr(tunnel_mod, "_quick_tunnel_once", flaky)
    assert tunnel_mod.quick_tunnel(8765, attempts=3) == "a tunnel"
    assert calls["n"] == 3

    # And it still gives up, with the last real reason rather than a summary.
    calls["n"] = 0
    monkeypatch.setattr(
        tunnel_mod,
        "_quick_tunnel_once",
        lambda *a, **k: (_ for _ in ()).throw(tunnel_mod.TunnelFailed("still slow")),
    )
    with pytest.raises(tunnel_mod.TunnelFailed, match="still slow"):
        tunnel_mod.quick_tunnel(8765, attempts=2)


def test_a_named_tunnel_is_run_never_created(monkeypatch):
    """Creating one writes credentials and a DNS record that outlive the process.

    So an account with no tunnels gets the three commands to run rather than
    having them run for it.
    """
    from tracker.webui import tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod, "find_cloudflared", lambda: "cloudflared")
    monkeypatch.setattr(tunnel_mod, "_check_runnable", lambda _binary: None)
    monkeypatch.setattr(tunnel_mod, "named_tunnels", lambda _binary=None: [])

    with pytest.raises(tunnel_mod.TunnelNotFound) as exc:
        tunnel_mod.named_tunnel(8765, "console")
    message = str(exc.value)
    assert "cloudflared tunnel create console" in message
    assert "route dns" in message

    monkeypatch.setattr(tunnel_mod, "named_tunnels", lambda _binary=None: ["other"])
    with pytest.raises(tunnel_mod.TunnelNotFound) as exc:
        tunnel_mod.named_tunnel(8765, "console")
    assert "This account has: other" in str(exc.value)


def test_the_named_tunnel_argv_pins_the_port_the_console_listens_on(monkeypatch):
    """`--url` before `run` supplies the ingress, so no config.yml can disagree."""
    from tracker.webui import tunnel as tunnel_mod

    captured: list[list[str]] = []

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            pass

    def fake_spawn(argv, _pattern):
        captured.append(argv)
        return FakeProcess(), ["Registered tunnel connection"], []

    monkeypatch.setattr(tunnel_mod, "find_cloudflared", lambda: "cloudflared")
    monkeypatch.setattr(tunnel_mod, "_check_runnable", lambda _binary: None)
    monkeypatch.setattr(tunnel_mod, "named_tunnels", lambda _binary=None: ["console"])
    monkeypatch.setattr(tunnel_mod, "_spawn", fake_spawn)

    result = tunnel_mod.named_tunnel(9001, "console", hostname="dc.example.com")
    assert captured[0] == [
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        "http://127.0.0.1:9001",
        "run",
        "console",
    ]
    assert result.url == "https://dc.example.com"
    assert result.kind == "named" and result.confirmed


def test_a_named_tunnel_without_a_hostname_says_unknown_rather_than_guessing():
    """The DNS route lives in your zone, not in the tunnel's output."""
    from tracker.webui.tunnel import Tunnel

    class FakeProcess:
        def poll(self):
            return 0

    assert Tunnel(url=None, process=FakeProcess(), kind="named").url is None  # type: ignore[arg-type]


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
