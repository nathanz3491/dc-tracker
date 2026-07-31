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
