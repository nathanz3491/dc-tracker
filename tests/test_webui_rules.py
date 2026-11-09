"""Rules that apply to a place, as the console shows them.

Three surfaces read `tracker.conditions`: a project's page ("Exposed to"), the Monitor
page, and the Rules view. The load-bearing assertions are that a rule changes nothing
about the project it reaches (`test_the_project_page_carries_its_rules_beside_it`), and
that it makes no followed project "blocked" (`test_monitor_lists_rules_apart_from_obstacles`).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading

import pytest
from sqlalchemy import select

from tests.test_conditions import NOW, PIQUA, _one, _page
from tests.test_webui import FAST_POLL, _account, as_reader, raw, request, sign_in
from tracker import conditions
from tracker.db import init_db, open_db, session_scope
from tracker.ingest.records import IngestRecord, RiskRecord, SourceRecord
from tracker.models import Condition, Event, Project
from tracker.upsert import upsert_record
from tracker.webui import rules as rules_view
from tracker.webui.server import Console, Handler

T0 = dt.datetime(2026, 11, 1, 12, 0, 0)

EXEMPTION = "Ohio sales-tax exemption for data center equipment"

#: name -> (company, city, county, state, phase, MW, an obstacle?)
CAMPUSES = {
    "Abilene Ridge": ("Acme", "Abilene", "Taylor", "TX", "announced", 400.0, True),
    "Temple Yard": ("Acme", "Temple", "Bell", "TX", "permitting", 200.0, False),
    "Piqua North": ("Beta", "Piqua", "Miami", "OH", "announced", 300.0, False),
}


def _campus(session, name: str) -> int:
    company, city, county, state, phase, mw, obstacle = CAMPUSES[name]
    url = f"https://news.example/{name.lower().replace(' ', '-')}"
    result = upsert_record(
        session,
        IngestRecord(
            project={"company": company, "name": name, "city": city, "state": state},
            sources=[
                SourceRecord(
                    url=url,
                    source_type="trade_press",
                    fetched_at=T0,
                    excerpt=f"{company} plans {mw:g} MW in {city}.",
                    claims={
                        "name": name,
                        "company": company,
                        "city": city,
                        "state": state,
                        "county": county,
                        "mw_planned": mw,
                        "phase": phase,
                    },
                    quotes={"mw_planned": f"plans {mw:g} MW"},
                )
            ],
            risks=(
                [
                    RiskRecord(
                        category="grid_capacity",
                        severity="material",
                        summary="The utility has not committed the load.",
                        quote="has not committed the load",
                        source_url=url,
                    )
                ]
                if obstacle
                else []
            ),
        ),
    )
    return result.project_id


@pytest.fixture
def ruled_db(tmp_path, migrated_copy):
    path = migrated_copy(tmp_path / "tracker.db")
    return path, seed(path)


def seed(path) -> dict[str, int]:
    """Three campuses and six rules: a Texas pledge on new permits, an ERCOT rule, a
    Piqua ban, an Ohio tax exemption (it eases), a transformer tariff (market) and a
    Texas bill voted down. Returns the campuses' ids by name.

    A function rather than the fixture's body so the same database can be built
    outside pytest, to look at the pages it draws.
    """
    engine, _ = init_db(path)
    with session_scope(engine) as session:
        ids = {name: _campus(session, name) for name in CAMPUSES}
        session.add(
            Event(
                project_id=ids["Temple Yard"],
                event_type="permit_approved",
                event_date=dt.date(2026, 9, 1),
                description="Approved.",
            )
        )
        session.flush()
        conditions.attach(session, _one(), {"rule": "a"}, _page(days=1), now=NOW)
        conditions.attach(
            session,
            _one(title="ERCOT large-load rule", kind="grid_rule", channel="grid_capacity",
                 scope="grid_region", state=None, region="ERCOT", status="in_force",
                 applies_to="unbuilt"),
            {"rule": "ERCOT now requires large loads to curtail."},
            _page(url="https://news.example/ercot", days=2), now=NOW,
        )  # fmt: skip
        conditions.attach(
            session,
            _one(title="Piqua ban", kind="ban", scope="place", state="OH", place="Piqua",
                 status="passed", min_mw=25, applies_to="unbuilt"),
            {
                "rule": "voters on Tuesday approved a ban on data centers over 25 megawatts",
                "place": "voters on Tuesday approved a ban on data centers over 25 megawatts",
                "status": "voters on Tuesday approved a ban on data centers over 25 megawatts",
            },
            _page(url="https://news.example/piqua", text=PIQUA, title="Piqua votes", days=3),
            now=NOW,
        )  # fmt: skip
        conditions.attach(
            session,
            _one(title="Transformer tariff", kind="tariff", channel="equipment_supply",
                 scope="input", state=None, input="large power transformers",
                 status="in_force"),
            {"rule": "a"}, _page(url="https://news.example/tariff", days=4), now=NOW,
        )  # fmt: skip
        conditions.attach(
            session,
            _one(title="Texas water-use bill", kind="law", channel="water", status="rejected",
                 quotes={"rule": "x"}),
            {"rule": "x"}, _page(url="https://news.example/water", days=5), now=NOW,
        )  # fmt: skip
        conditions.attach(
            session,
            _one(title=EXEMPTION, kind="tax", channel="financing", state="OH",
                 status="in_force", applies_to="all", effect="eases"),
            {"rule": "a"}, _page(url="https://news.example/ohio-tax", days=6), now=NOW,
        )  # fmt: skip
        conditions.rebuild(session, now=NOW)
    return ids


def _serve(path):
    from http.server import ThreadingHTTPServer

    console = Console(path)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs=FAST_POLL, daemon=True).start()
    return httpd, console


@pytest.fixture
def open_console(ruled_db):
    """No accounts: an open console, as on loopback."""
    path, ids = ruled_db
    httpd, console = _serve(path)
    try:
        yield httpd.server_address, ids
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


@pytest.fixture
def gated_reader(ruled_db):
    """One account, and a cookie for it."""
    path, ids = ruled_db
    _account(path)
    httpd, console = _serve(path)
    try:
        status, cookie = sign_in(httpd.server_address)
        assert status == 200, "the fixture could not sign in"
        yield httpd.server_address, cookie, ids
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()


def _titles(rows) -> list[str]:
    return [r["title"] for r in rows]


# --- a project's page ---------------------------------------------------------------


def test_the_project_page_carries_its_rules_beside_it(open_console, ruled_db):
    """Each rule with where, status, need, fit and why, and its articles with their
    sentences — and the project itself exactly as `project_payload` builds it, so no
    obstacle count, blocker or confidence has moved."""
    from tracker.webui.dataset import project_payload

    address, ids = open_console
    status, body = request(address, f"/api/project?id={ids['Abilene Ridge']}")
    assert status == 200

    assert "exposures" not in body["project"]
    with session_scope(open_db(ruled_db[0]), commit=False) as session:
        built = project_payload(session.get(Project, ids["Abilene Ridge"]), claims=True)
        assert body["project"] == json.loads(json.dumps(built, ensure_ascii=False, default=str))
    assert [r["category"] for r in body["project"]["risks"]] == ["grid_capacity"]

    by_title = {e["title"]: e for e in body["exposures"]}
    assert set(by_title) == {"Texas pause on new data center permits", "ERCOT large-load rule"}
    pause = by_title["Texas pause on new data center permits"]
    assert (pause["where"], pause["status_label"], pause["need_label"]) == (
        "Texas",
        "pledged",
        "Permitting",
    )
    assert pause["fit"] == "exact" and "no permit on record" in pause["why"]
    assert "permit approved first" in pause["clears"]
    assert pause["last_reported"] == (NOW - dt.timedelta(days=1)).date().isoformat()
    [article] = pause["sources"]
    assert article["url"] == "https://news.example/tx" and article["publisher"]
    assert [q["for"] for q in article["quotes"]] == [["the rule"]]

    ercot = by_title["ERCOT large-load rule"]
    assert ercot["fit"] == "likely" and "mostly ERCOT" in ercot["why"]
    assert ercot["status_label"] == "in force"


def test_a_sentence_kept_for_several_things_is_listed_once(open_console):
    address, ids = open_console
    _status, body = request(address, f"/api/project?id={ids['Piqua North']}")
    ban = {e["title"]: e for e in body["exposures"]}["Piqua ban"]
    assert ban["fit"] == "likely" and "town limits not checked" in ban["why"]
    assert ban["covers"] == "projects not yet running of 25 MW and up"
    [article] = ban["sources"]
    [sentence] = article["quotes"]
    assert sentence["for"] == ["the rule", "where", "where it stands"]


def test_a_rule_that_eases_building_has_nothing_to_clear(open_console):
    """Ohio's exemption reaches the Piqua campus as the ban does, says it eases, and
    carries no "what would clear it": help is not a threat to be cleared."""
    address, ids = open_console
    _status, body = request(address, f"/api/project?id={ids['Piqua North']}")
    by_title = {e["title"]: e for e in body["exposures"]}
    exemption, ban = by_title[EXEMPTION], by_title["Piqua ban"]
    assert (exemption["effect"], exemption["effect_label"]) == ("eases", "eases")
    assert exemption["fit"] == "exact" and exemption["clears"] == ""
    assert ban["effect"] == "restricts" and ban["clears"].startswith("the rule repealed")


def test_a_rule_lifted_since_the_last_match_is_not_shown(ruled_db):
    """The exposures are rebuilt by `conditions match`, not on every read, so a rule
    read as lifted keeps its old rows until then — and reaches nothing."""
    path, ids = ruled_db
    with session_scope(open_db(path, readonly=False), commit=False) as session:
        conditions.attach(
            session, _one(status="lifted", quotes={"rule": "x"}), {"rule": "x"},
            _page(url="https://news.example/lifted"), now=NOW,
        )  # fmt: skip
        session.flush()
        titles = _titles(rules_view.exposures(session, ids["Abilene Ridge"]))
    assert titles == ["ERCOT large-load rule"]


def test_a_link_that_is_not_http_is_never_handed_to_the_page(ruled_db):
    path, _ids = ruled_db
    with session_scope(open_db(path, readonly=False), commit=False) as session:
        source = session.scalars(select(Condition)).first().sources[0]
        source.url = "javascript:alert(1)"
        assert rules_view.source_json(source)["url"] is None


# --- the Rules view -----------------------------------------------------------------


def test_the_rules_route_lists_rules_and_the_market_apart(open_console):
    address, _ids = open_console
    status, body = request(address, "/api/rules")
    assert status == 200
    reach = {r["title"]: r["projects"] for r in body["rules"]}
    assert reach == {
        "Texas pause on new data center permits": 1,  # Temple Yard has its permit
        "ERCOT large-load rule": 2,
        "Piqua ban": 1,
        EXEMPTION: 1,
    }
    assert _titles(body["market"]) == ["Transformer tariff"]
    assert body["market"][0]["market"] is True and body["market"][0]["projects"] is None
    # Help is counted apart from restriction, never added to it.
    counts = body["counts"]
    assert (counts["restricts"], counts["eases"]) == (3, 1)
    assert (counts["projects"], counts["helped"], counts["in_force"]) == (3, 1, 1)
    assert {s["state"] for s in body["states"]} == {"TX", "OH"}

    _status, history = request(address, "/api/rules?cleared=1")
    voted = {r["title"]: r for r in history["rules"]}["Texas water-use bill"]
    assert voted["status_label"] == "voted down" and voted["projects"] == 0


def test_a_states_filter_takes_in_its_grid_regions_rules(open_console):
    address, _ids = open_console
    _status, texas = request(address, "/api/rules?state=tx")
    assert set(_titles(texas["rules"])) == {
        "Texas pause on new data center permits",
        "ERCOT large-load rule",
    }
    assert texas["state"] == "TX"
    _status, ohio = request(address, "/api/rules?state=OH")
    assert _titles(ohio["rules"]) == ["Piqua ban", EXEMPTION], "newest report first"
    assert request(address, "/api/rules?state=Texas")[0] == 400


def test_one_rule_lists_every_project_it_reaches_with_fit_and_why(open_console):
    address, ids = open_console
    _status, listing = request(address, "/api/rules")
    ercot = {r["title"]: r["id"] for r in listing["rules"]}["ERCOT large-load rule"]

    status, body = request(address, f"/api/rule?id={ercot}")
    assert status == 200
    assert body["rule"]["title"] == "ERCOT large-load rule"
    assert {p["project_id"] for p in body["projects"]} == {
        ids["Abilene Ridge"],
        ids["Temple Yard"],
    }
    assert all(p["fit"] == "likely" and p["why"] for p in body["projects"])
    assert body["projects"][0]["company"] == "Acme"

    assert request(address, "/api/rule?id=99999")[0] == 404
    assert request(address, "/api/rule?id=nope")[0] == 400


def test_the_rules_routes_and_page_need_a_sign_in(gated_reader):
    address, cookie, _ids = gated_reader
    for path in ("/api/rules", "/api/rules?state=TX", "/api/rule?id=1"):
        assert raw(address, path)[0] == 401, path
        assert raw(address, path, cookie=cookie)[0] == 200, path

    status, headers, _ = raw(address, "/rules")
    assert status == 303 and headers["Location"] == "/signin?next=/rules"
    status, _headers, page = raw(address, "/rules", cookie=cookie)
    assert status == 200 and 'window.DC_VIEW="rules"' in page


def test_the_rules_view_is_drawn_by_its_own_module():
    """app.js imports it and puts it in the nav; the route list test checks the nav."""
    from tracker.webui import assets

    app = (assets.STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    imported = re.search(r'import \{([^}]*)\} from "/static/views-rules\.js";', app)
    assert imported and "RulesView" in imported.group(1)
    assert (assets.STATIC_ROOT / "views-rules.js").is_file()


# --- Monitor ------------------------------------------------------------------------


def test_monitor_lists_rules_apart_from_obstacles(gated_reader):
    """Each rule once, with the followed projects it reaches and what would clear it.
    A project a rule reaches but no obstacle holds is not "blocked"."""
    address, cookie, ids = gated_reader
    as_reader(address, cookie, "/api/watch", "POST", {"action": "add", "entry": "Acme"})

    status, body = as_reader(address, cookie, "/api/monitor")
    assert status == 200
    counts = body["counts"]
    assert (counts["projects"], counts["blocked"], counts["blockers"]) == (2, 1, 1)
    assert (counts["rules"], counts["exposed"]) == (2, 2)

    by_title = {r["title"]: r for r in body["rules"]}
    pause = by_title["Texas pause on new data center permits"]
    assert [p["project_id"] for p in pause["projects"]] == [ids["Abilene Ridge"]]
    assert pause["projects"][0]["fit"] == "exact"
    assert pause["clears"].startswith("the pledge dropped")
    assert "permit approved first" in pause["clears"]
    ercot = by_title["ERCOT large-load rule"]
    assert {p["project_id"] for p in ercot["projects"]} == set(ids.values()) - {ids["Piqua North"]}
    assert "permit" not in ercot["clears"], "it covers every unbuilt project, permit or none"

    cards = {p["project"]: p for p in body["projects"]}
    assert [b["category"] for b in cards["Abilene Ridge"]["blockers"]] == ["grid_capacity"]
    assert cards["Temple Yard"]["blockers"] == [] and cards["Temple Yard"]["worst"] is None
    assert _titles(cards["Temple Yard"]["rules"]) == ["ERCOT large-load rule"]


def test_monitor_counts_a_rule_that_eases_apart_and_gives_it_nothing_to_clear(gated_reader):
    address, cookie, ids = gated_reader
    as_reader(address, cookie, "/api/watch", "POST", {"action": "add", "entry": "Beta"})

    _status, body = as_reader(address, cookie, "/api/monitor")
    counts = body["counts"]
    assert (counts["blocked"], counts["rules"], counts["exposed"]) == (0, 1, 1)
    assert (counts["easing"], counts["helped"]) == (1, 1)
    by_title = {r["title"]: r for r in body["rules"]}
    assert by_title[EXEMPTION]["effect"] == "eases" and by_title[EXEMPTION]["clears"] == ""
    assert by_title["Piqua ban"]["clears"], "a restriction says what would clear it"
    assert [p["project_id"] for p in by_title[EXEMPTION]["projects"]] == [ids["Piqua North"]]


def test_the_monitor_tally_on_updates_is_unchanged_by_rules(gated_reader):
    address, cookie, _ids = gated_reader
    as_reader(address, cookie, "/api/watch", "POST", {"action": "add", "entry": "Acme"})
    _status, body = as_reader(address, cookie, "/api/updates?days=36500")
    assert (body["watch"]["blocked"], body["watch"]["blockers"]) == (1, 1)
