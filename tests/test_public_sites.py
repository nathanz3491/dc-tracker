"""The status page and the public map: what they compute, and what they let out.

In the order a mistake would cost:

* the map sends a stranger exactly the fields its level allows — never investment,
  a tenant, a date, an obstacle or a source — and counts each campus once;
* each hostname answers only its own pages: nothing of the console, the API or the
  docs is reachable on the status or map hostnames, and neither answers a write;
* the status page counts a stretch with no checks as downtime, names a restart a
  restart, and reads the nightly log the way the loop writes it.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from tracker.db import init_db, session_scope
from tracker.models import Project
from tracker.webui import publicmap, statussite
from tracker.webui.server import Console, Handler
from tracker.webui.statussite import Check, Store

UTC = dt.UTC
STATUS_HOST = "status.console.example"
MAP_HOST = "map.console.example"
API_HOST = "api.console.example"
DOCS_HOST = "docs.console.example"
CONSOLE_HOST = "console.example"


# --- the map's payload ------------------------------------------------------------


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
        _campus(
            session,
            1,
            mw_planned=1200.0,
            mw_built=200.0,
            investment_usd=15_000_000_000,
            customer="BigCo",
            blocker="The substation is late.",
        )
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


def test_each_level_sends_exactly_its_fields(db):
    """The allowlist is the whole of what a stranger gets; a key outside it is a leak."""
    engine, _ = init_db(db)
    with session_scope(engine, commit=False) as session:
        for level in publicmap.LEVELS:
            data = publicmap.build(session, level=level)
            for point in data["points"]:
                assert set(point) == set(publicmap.FIELDS[level]), level
            blob = json.dumps(data)
            for secret in (
                "15000000000",
                "BigCo",
                "substation",
                "investment",
                "customer",
                "blocker",
            ):
                assert secret not in blob, (level, secret)


def test_states_level_draws_no_dots_and_dots_level_no_names(db):
    engine, _ = init_db(db)
    with session_scope(engine, commit=False) as session:
        assert publicmap.build(session, level="states")["points"] == []
        dots = publicmap.build(session, level="dots")["points"]
        assert all("name" not in p and "mw" not in p and "place" not in p for p in dots)
        assert {p["band"] for p in dots} == {"1000_2999", "100_299", "300_999", None}
        assert all(p["lat"] == round(p["lat"], 1) for p in dots)
        campuses = publicmap.build(session, level="campuses")["points"]
        assert {p["name"] for p in campuses} >= {"Campus 1", "Campus 2"}


def test_totals_leave_out_cancelled_campuses_but_the_map_draws_them(db):
    engine, _ = init_db(db)
    with session_scope(engine, commit=False) as session:
        data = publicmap.build(session, level="dots")
    assert data["totals"] == {
        "campuses": 4,
        "planned_mw": 1500.0,
        "operating_mw": 450.0,
        "states": 4,
    }
    assert data["stages"]["cancelled"] == 1
    assert any(p["stage"] == "cancelled" for p in data["points"])
    assert data["states"]["VA"] == {"campuses": 1, "planned_mw": 250.0}
    # No coordinates, no dot — but still counted.
    assert data["states"]["WY"]["campuses"] == 1
    assert len(data["points"]) == 4


def test_a_suspected_duplicate_is_counted_and_drawn_once(db, monkeypatch):
    engine, _ = init_db(db)
    with session_scope(engine, commit=False) as session:
        ids = {p.name: p.id for p in session.query(Project)}
        monkeypatch.setattr(
            publicmap,
            "counted_ids",
            lambda _s: ({ids["Campus 1"], ids["Campus 4"]}, {ids["Campus 2"]}),
        )
        data = publicmap.build(session, level="campuses")
    assert "Campus 2" not in {p["name"] for p in data["points"]}
    assert data["totals"]["campuses"] == 2


def test_the_written_summary_says_not_published_rather_than_zero(db):
    engine, _ = init_db(db)
    with session_scope(engine, commit=False) as session:
        html = publicmap.summary_html(publicmap.build(session, level="dots"))
    assert "<dd>4</dd>" in html and "1.5 GW" in html
    assert "OH</th><td>1</td><td>not published</td>" in html


def test_bands():
    assert publicmap.band(None) is None and publicmap.band(0) is None
    assert publicmap.band(99.9) == "under_100"
    assert publicmap.band(100) == "100_299"
    assert publicmap.band(3000) == "3000_plus"


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


@pytest.fixture
def live(db):
    from http.server import ThreadingHTTPServer

    console = Console(
        db,
        published=True,
        console_host=CONSOLE_HOST,
        docs_host=DOCS_HOST,
        api_host=API_HOST,
        status_host=STATUS_HOST,
        map_host=MAP_HOST,
    )
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
    assert f'<meta name="dc-map" content="https://{MAP_HOST}/">' in body
    status, _, body = call(live, "/status.json", STATUS_HOST)
    assert status == 200 and json.loads(body)["overall"] == "unknown"
    status, _, _ = call(live, "/static/pages/status.js", STATUS_HOST)
    assert status == 200
    for path in ("/api/dataset", "/api/v1/health", "/docs/", "/signin", "/map/", "/static/app.js"):
        assert call(live, path, STATUS_HOST)[0] == 404, path
    assert call(live, "/api/login", STATUS_HOST, method="POST")[0] == 404


def test_the_map_hostname_is_the_map_and_nothing_else(live):
    status, headers, body = call(live, "/", MAP_HOST)
    assert status == 200
    assert f'<link rel="canonical" href="https://{MAP_HOST}/">' in body
    assert "<dd>4</dd>" in body, "the figures are written into the page for search engines"
    assert "X-Robots-Tag" not in headers
    status, _, body = call(live, "/data.json", MAP_HOST)
    data = json.loads(body)
    assert status == 200 and data["level"] == "dots"
    assert all(set(p) == set(publicmap.FIELDS["dots"]) for p in data["points"])
    status, _, body = call(live, "/robots.txt", MAP_HOST)
    assert status == 200 and f"Sitemap: https://{MAP_HOST}/sitemap.xml" in body
    assert call(live, "/sitemap.xml", MAP_HOST)[0] == 200
    assert call(live, "/static/vendor/us-states-10m.json", MAP_HOST)[0] == 200
    for path in (
        "/api/dataset",
        "/api/v1/projects",
        "/docs/",
        "/signin",
        "/status/",
        "/static/app.js",
    ):
        assert call(live, path, MAP_HOST)[0] == 404, path
    assert call(live, "/api/login", MAP_HOST, method="POST")[0] == 404


def test_both_are_public_under_a_path_on_the_console(live):
    """Behind the sign-in the console needs, but these two are for anybody."""
    assert call(live, "/status/", CONSOLE_HOST)[0] == 200
    assert call(live, "/public-map/", CONSOLE_HOST)[0] == 200
    assert call(live, "/public-map/data.json", CONSOLE_HOST)[0] == 200
    # `/map` stays the console's own Map view, behind the sign-in.
    assert call(live, "/map", CONSOLE_HOST)[0] != 200
    assert call(live, "/status", CONSOLE_HOST)[0] == 303
    # Not on the other public hostnames: the API claims its whole host, the docs theirs.
    assert call(live, "/status/", API_HOST)[0] == 404
    assert call(live, "/public-map/", DOCS_HOST)[0] == 404
    # And the console's data stays behind the sign-in.
    assert call(live, "/api/dataset", CONSOLE_HOST)[0] == 401
