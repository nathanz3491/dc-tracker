"""The status page, the console map's figures, and the hostnames: what they compute,
and what they let out.

In the order a mistake would cost:

* each hostname answers only its own pages: nothing of the console, the API or the
  docs is reachable on the status hostname, and it answers no write; a hostname this
  server does not serve (the retired public map's) is sent to the console rather
  than answered as it;
* the Map view's totals count what the Capex page counts — no paused or cancelled
  campus, each suspected duplicate once — and draw a duplicate once;
* the status page counts a stretch with no checks as downtime, names a restart a
  restart, and reads the nightly log the way the loop writes it.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path

import pytest

from tracker.db import init_db, session_scope
from tracker.models import Project
from tracker.webui import mapsummary, statussite
from tracker.webui.server import Console, Handler
from tracker.webui.statussite import Check, Store

UTC = dt.UTC
STATUS_HOST = "status.console.example"
API_HOST = "api.console.example"
DOCS_HOST = "docs.console.example"
CONSOLE_HOST = "console.example"
RETIRED_HOST = "map.console.example"


# --- the Map view's figures -------------------------------------------------------


def _campus(session, n: int, **kw) -> Project:
    project = Project(
        name=kw.pop("name", f"Campus {n}"),
        company=kw.pop("company", "Acme"),
        state=kw.pop("state", "TX"),
        city=kw.pop("city", "Abilene"),
        dedup_key=f"key-{n}",
        phase=kw.pop("phase", "construction"),
        lat=kw.pop("lat", 32.4487),
        lon=kw.pop("lon", -99.7331),
        **kw,
    )
    session.add(project)
    session.flush()
    return project


@pytest.fixture
def db(tmp_path, migrated_copy):
    path = migrated_copy(tmp_path / "tracker.db")
    engine, _ = init_db(path)
    with session_scope(engine) as session:
        _campus(session, 1, mw_planned=1200.0, mw_built=200.0)
        _campus(
            session,
            2,
            state="VA",
            city="Ashburn",
            lat=39.0438,
            lon=-77.4874,
            mw_planned=250.0,
            phase="operational",
            mw_built=250.0,
        )
        _campus(
            session,
            3,
            state="VA",
            city="Leesburg",
            lat=39.1157,
            lon=-77.5636,
            mw_planned=600.0,
            phase="cancelled",
        )
        _campus(session, 4, state="OH", city="Columbus", lat=39.9612, lon=-82.9988)
        _campus(session, 5, state="WY", lat=None, lon=None, mw_planned=50.0)
    return path


def test_totals_leave_out_a_cancelled_campus_but_its_stage_is_counted(db):
    """The figures are the Capex page's; the stage chips count every row drawn."""
    engine, _ = init_db(db)
    with session_scope(engine, commit=False) as session:
        data = mapsummary.build(session)
    assert data["totals"] == {
        "campuses": 4,
        "planned_mw": 1500.0,
        "operating_mw": 450.0,
        "states": 4,
    }
    assert data["stages"]["cancelled"] == 1
    assert data["stages"]["construction"] == 3
    assert list(data["stages"]) == list(mapsummary.STAGES)
    assert data["states"]["VA"] == {"campuses": 1, "planned_mw": 250.0}
    # A campus with no coordinates draws no dot, but it is still counted.
    assert data["states"]["WY"] == {"campuses": 1, "planned_mw": 50.0}
    # A size nobody published counts as zero, not as a gap in the table.
    assert data["states"]["OH"] == {"campuses": 1, "planned_mw": 0.0}
    assert data["skipped"] == []
    assert data["updated_at"]


def test_a_suspected_duplicate_is_counted_once_and_not_drawn(db, monkeypatch):
    """Two dots for one campus would be the double count again, in a picture."""
    engine, _ = init_db(db)
    with session_scope(engine, commit=False) as session:
        ids = {p.name: p.id for p in session.query(Project)}
        monkeypatch.setattr(
            mapsummary,
            "counted_ids",
            lambda _s: ({ids["Campus 1"], ids["Campus 4"]}, {ids["Campus 2"]}),
        )
        data = mapsummary.build(session)
    assert data["skipped"] == [ids["Campus 2"]]
    assert data["totals"]["campuses"] == 2
    assert "VA" not in data["states"]
    assert data["stages"]["operational"] == 0


# --- the status numbers -----------------------------------------------------------

T = dt.datetime(2026, 10, 17, 4, 0, tzinfo=UTC)
TZ = dt.timezone(dt.timedelta(hours=8))
STEP = dt.timedelta(minutes=5)


def _store(tmp_path: Path, rounds, *, starts=(), notes=()) -> Store:
    store = Store(tmp_path / "status.db")
    for at, booted in starts:
        store.started(at, booted)
    for at, results in rounds:
        store.record(at, results)
    for note in notes:
        store.add_note(**note)
    return store


def _ok(service="console", ms=200):
    return Check(service, True, 200, ms)


def _down(service="console"):
    return Check(service, False, 502, None, "HTTP 502")


def _payload(store, now, services=("console",)):
    return statussite.payload(
        store, services=list(services), interval_s=300, nights=[], data={}, links={}, now=now, tz=TZ
    )


def test_a_stretch_with_no_checks_is_downtime_and_a_reboot_is_a_restart(tmp_path):
    """The 2026-10-16 reset: ten minutes of nothing, then a fresh boot. One missed
    round is enough to call it; counting it as "no data" instead would report the
    best uptime on the worst day."""
    rounds = [(T + STEP * i, [_ok()]) for i in range(12)]
    rounds += [(T + STEP * 11 + dt.timedelta(minutes=11) + STEP * i, [_ok()]) for i in range(12)]
    booted = T + STEP * 11 + dt.timedelta(minutes=8)
    store = _store(tmp_path, rounds, starts=[(T, None), (booted + dt.timedelta(minutes=2), booted)])
    data = _payload(store, rounds[-1][0] + STEP)
    [gap] = [i for i in data["incidents"] if i["kind"] in ("restart", "offline")]
    assert gap["kind"] == "restart"
    assert gap["booted_at"] == booted.isoformat().replace("+00:00", "Z")
    assert data["services"][0]["uptime"] < 1.0
    assert data["overall"] == "operational"


def test_rounds_a_little_late_are_not_a_gap(tmp_path):
    rounds = [(T + (STEP + dt.timedelta(seconds=50)) * i, [_ok()]) for i in range(30)]
    data = _payload(_store(tmp_path, rounds), rounds[-1][0] + STEP)
    assert not [i for i in data["incidents"] if i["kind"] in ("restart", "offline")]
    assert data["services"][0]["uptime"] == 1.0


def test_two_failures_running_are_an_outage_and_one_is_degraded(tmp_path):
    rounds = [(T, [_ok(), _ok("api")]), (T + STEP, [_ok(), _down("api")])]
    now = T + STEP * 2
    data = _payload(_store(tmp_path, rounds), now, services=("console", "api"))
    assert {s["id"]: s["state"] for s in data["services"]} == {
        "console": "operational",
        "api": "degraded",
    }
    assert data["overall"] == "degraded"

    rounds.append((T + STEP * 2, [_ok(), _down("api")]))
    data = _payload(_store(tmp_path / "b", rounds), T + STEP * 3, services=("console", "api"))
    assert data["overall"] == "partial_outage"
    [outage] = [i for i in data["incidents"] if i["kind"] == "outage"]
    assert outage["services"] == ["api"] and outage["end"] is None


def test_silence_while_the_page_is_served_is_unknown(tmp_path):
    rounds = [(T, [_ok()])]
    data = _payload(_store(tmp_path, rounds), T + dt.timedelta(hours=1))
    assert data["overall"] == "unknown"
    assert data["incidents"][0]["kind"] == "silent"


def test_a_slow_answer_is_degraded(tmp_path):
    data = _payload(_store(tmp_path, [(T, [_ok(ms=9000)])]), T + STEP)
    assert data["services"][0]["state"] == "degraded"


def test_days_are_bucketed_in_the_hosts_zone_and_unchecked_days_say_so(tmp_path):
    rounds = [(T + STEP * i, [_ok()]) for i in range(10)]
    data = _payload(_store(tmp_path, rounds), rounds[-1][0] + STEP)
    days = data["services"][0]["days"]
    assert len(days) == statussite.HISTORY_DAYS
    assert days[-1]["date"] == (T + STEP * 10).astimezone(TZ).date().isoformat()
    assert days[-1]["uptime"] == 1.0
    assert days[0]["uptime"] is None


def test_a_note_is_shown_and_can_be_removed(tmp_path):
    store = _store(
        tmp_path,
        [(T, [_ok()])],
        notes=[
            {
                "title": "Moved the server",
                "title_zh": "服务器搬迁",
                "starts_at": T,
                "ends_at": T + STEP,
            }
        ],
    )
    data = _payload(store, T + STEP)
    [note] = [i for i in data["incidents"] if i["kind"] == "note"]
    assert note["title"] == "Moved the server" and note["title_zh"] == "服务器搬迁"
    assert store.remove_note(store.notes()[0].id)
    assert not [i for i in _payload(store, T + STEP)["incidents"] if i["kind"] == "note"]
    with pytest.raises(ValueError):
        store.add_note("x", service="nonsense")
    with pytest.raises(ValueError):
        store.add_note("  ")


def test_the_prober_records_a_start_and_a_round(tmp_path):
    store = Store(tmp_path / "status.db")
    prober = statussite.Prober(
        store,
        [("console", "https://console.example/"), ("api", "https://api.console.example/v1/health")],
        interval_s=60,
        checker=lambda service, url: Check(service, service == "console", 200, 5),
        clock=lambda: T,
    )
    prober.halt.set()  # run() checks once before it first waits, then sees the halt
    prober.run()
    assert store.starts_since(T - STEP)[0][0] == T
    rounds = store.checks_since(T - STEP)
    assert [(c.service, c.ok) for _at, c in rounds] == [("console", True), ("api", False)]


def test_the_first_round_waits_for_the_server_to_be_listening(tmp_path):
    """The first deploy checked before the port was serving and recorded its own
    restart as an outage. The start is recorded at once; the round waits."""
    store = Store(tmp_path / "status.db")
    prober = statussite.Prober(
        store,
        [("console", "https://console.example/")],
        interval_s=60,
        checker=lambda service, url: pytest.fail("checked before the delay ended"),
        clock=lambda: T,
        first_delay_s=30,
    )
    prober.halt.set()  # stopped during the delay
    prober.run()
    assert len(store.starts_since(T - STEP)) == 1
    assert store.checks_since(T - STEP) == []


def test_a_check_reports_status_and_failure():
    import httpx

    def handler(request):
        return httpx.Response(200 if request.url.path == "/" else 503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        good = statussite.check("console", "https://console.example/", client=client)
        bad = statussite.check("api", "https://api.console.example/v1/health", client=client)
    assert good.ok and good.http_status == 200 and good.latency_ms is not None
    assert not bad.ok and bad.error == "HTTP 503"


# --- the nightly log --------------------------------------------------------------

LOG = """\
[2026-10-14 18:01:05] overnight starting  (pid 1)
--- 18:01:05  free — dates
[2026-10-14 20:10:00] 2 rounds with no reduction — this is the fixed point, stopping
[2026-10-14 20:21:45] overnight complete — 140m, ¥5.53, ~6677461 tokens
[2026-10-15 18:01:02] overnight starting  (pid 2)
[2026-10-15 19:00:00] spend ceiling reached (¥12.01 of ¥12) before enrich — stopping
[2026-10-15 19:04:55] overnight complete — 63m, ¥12.01, ~1 tokens
[2026-10-16 18:01:01] overnight starting  (pid 3)
--- 18:03:41  enrich — 40 row(s)
[2026-10-16 23:50:00] overnight starting  (pid 4)
[2026-10-17 18:01:00] overnight starting  (pid 5)
--- 23:58:00  discover
--- 00:10:00  settle
"""


def test_the_nightly_log_reads_as_nights():
    nights = statussite.parse_nights(LOG.splitlines(True), tz=TZ)
    assert [n.outcome for n in nights] == ["complete", "complete"] + ["interrupted"] * 3
    assert [n.stop for n in nights[:2]] == ["settled", "spend_limit"]
    assert nights[0].minutes == 140
    # 23:50 came almost six hours after 18:01: not a hand restart, so 18:01 was cut
    # off, and so was 23:50, which a whole day separates from the next.
    assert [n.started_at.hour for n in nights[2:]] == [18, 23, 18]
    assert nights[2].minutes == 2
    # A phase line past midnight belongs to the next day.
    assert nights[4].last_seen_at == dt.datetime(2026, 10, 18, 0, 10, tzinfo=TZ)


def test_a_start_restarted_within_hours_is_not_a_cut_off_night():
    text = (
        "[2026-10-05 11:38:00] overnight starting\n"
        "[2026-10-05 12:01:00] overnight starting  (pid 9)\n"
        "[2026-10-05 13:22:00] overnight complete — 81m, ~1 tokens\n"
    )
    nights = statussite.parse_nights(text.splitlines(True), tz=TZ)
    assert [(n.started_at.hour, n.outcome) for n in nights] == [(12, "complete")]


def test_the_last_night_is_running_while_the_loop_holds_its_lock():
    nights = statussite.parse_nights(LOG.splitlines(True), tz=TZ, running=True)
    assert nights[-1].outcome == "running"


def test_a_quiet_night_takes_its_last_moment_from_its_spend_ledger(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "overnight.log").write_text(
        "[2026-10-16 18:01:01] overnight starting  (pid 3)\n--- 18:03:41  enrich\n",
        encoding="utf-8",
    )
    (runs / "overnight-20261016-180101.spend").write_text(
        "2026-10-16T10:05:27Z\t3\tenrich\tm\t1\t1\t0\t1\textract\n"
        "2026-10-16T11:26:07Z\t3\tenrich\tm\t1\t1\t0\t1\tagent\n",
        encoding="utf-8",
    )
    statussite._night_cache.clear()
    [night] = statussite.read_nights(runs)
    assert night.last_seen_at.astimezone(UTC) == dt.datetime(2026, 10, 16, 11, 26, 7, tzinfo=UTC)
    assert night.outcome == "interrupted"


# --- the hostnames ----------------------------------------------------------------


@contextmanager
def _serving(console):
    from http.server import ThreadingHTTPServer

    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


@pytest.fixture
def live(db):
    console = Console(
        db,
        published=True,
        console_host=CONSOLE_HOST,
        docs_host=DOCS_HOST,
        api_host=API_HOST,
        status_host=STATUS_HOST,
    )
    with _serving(console) as address:
        yield address


def call(address, path, host=None, method="GET"):
    conn = HTTPConnection(*address, timeout=30)
    headers = {"Host": host} if host else {}
    conn.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
    response = conn.getresponse()
    body = response.read().decode("utf-8", "replace")
    conn.close()
    return response.status, dict(response.getheaders()), body


def test_the_status_hostname_is_the_status_page_and_nothing_else(live):
    status, headers, body = call(live, "/", STATUS_HOST)
    assert status == 200 and "dc-tracker status" in body
    assert headers["X-Robots-Tag"] == "noindex"
    assert 'name="dc-map"' not in body, "the public map is gone"
    assert f'<meta name="dc-docs" content="https://{DOCS_HOST}/">' in body
    status, _, body = call(live, "/status.json", STATUS_HOST)
    data = json.loads(body)
    assert status == 200 and data["overall"] == "unknown"
    assert [s["id"] for s in data["services"]] == ["console", "api", "docs"]
    status, _, _ = call(live, "/static/pages/status.js", STATUS_HOST)
    assert status == 200
    for path in ("/api/dataset", "/api/map", "/api/v1/health", "/docs/", "/signin", "/map"):
        assert call(live, path, STATUS_HOST)[0] == 404, path
    assert call(live, "/static/app.js", STATUS_HOST)[0] == 404
    assert call(live, "/api/login", STATUS_HOST, method="POST")[0] == 404


def test_the_status_page_is_public_under_a_path_on_the_console(live):
    """Behind the sign-in the console needs, but the status page is for anybody."""
    assert call(live, "/status/", CONSOLE_HOST)[0] == 200
    assert call(live, "/status", CONSOLE_HOST)[0] == 303
    # Not on the API's hostname: it claims every path there.
    assert call(live, "/status/", API_HOST)[0] == 404
    # The retired public map's path is nothing now, and the Map view and its
    # figures stay behind the sign-in.
    assert call(live, "/public-map/", CONSOLE_HOST)[0] != 200
    assert call(live, "/map", CONSOLE_HOST)[0] != 200
    assert call(live, "/api/map", CONSOLE_HOST)[0] == 401
    assert call(live, "/api/dataset", CONSOLE_HOST)[0] == 401


def test_a_hostname_this_server_does_not_serve_is_sent_to_the_console(live):
    """The public map's DNS record outlives its site. Answered as the console, it
    would serve the sign-in page — and set its cookie — under a name that is not
    the console's."""
    status, headers, body = call(live, "/map?stage=construction", RETIRED_HOST)
    assert status == 307
    assert headers["Location"] == f"https://{CONSOLE_HOST}/map?stage=construction"
    assert body == "" and "Set-Cookie" not in headers
    assert call(live, "/", "MAP.console.example:443")[1]["Location"] == f"https://{CONSOLE_HOST}/"
    status, headers, _ = call(live, "/api/login", RETIRED_HOST, method="POST")
    assert status == 307 and headers["Location"] == f"https://{CONSOLE_HOST}/api/login"


@pytest.mark.parametrize(
    "host",
    [None, "localhost:8765", "127.0.0.1", "[::1]:8765", "10.0.0.5", CONSOLE_HOST, DOCS_HOST],
)
def test_this_machine_and_the_known_sites_are_never_redirected(live, host):
    """The deployer's health check asks an address, and the other sites are ours."""
    assert call(live, "/signin", host)[0] != 307


def test_without_a_console_hostname_nothing_is_redirected(db):
    """A laptop or a quick tunnel has no name to send anybody to."""
    with _serving(Console(db)) as address:
        assert call(address, "/api/map", RETIRED_HOST)[0] == 200
