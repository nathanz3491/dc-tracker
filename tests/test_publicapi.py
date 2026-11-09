"""The public API: a key is the only way in, the answers are its own documented
shapes, and the API hostname is nothing but the API.

The properties, in the order a mistake would cost:

* without a valid key there is no data — not with a console session either;
* a revoked key, or a disabled account's key, stops working at once;
* nothing on the API answers a write, and nothing of the console answers on the
  API hostname;
* a caller that loops is slowed, not the console;
* each field of a campus says where its value came from.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from http.client import HTTPConnection

import pytest

from tracker import accounts, apikeys
from tracker.db import init_db, open_db, session_scope
from tracker.ingest.records import IngestRecord, RiskRecord, SourceRecord
from tracker.upsert import upsert_record
from tracker.webui import publicapi
from tracker.webui.server import Console, Handler

API_HOST = "api.console.example"
T0 = dt.datetime(2026, 9, 1, 12, 0)


@pytest.fixture
def db(tmp_path, migrated_copy):
    path = migrated_copy(tmp_path / "tracker.db")
    engine, _ = init_db(path)
    with session_scope(engine) as session:
        upsert_record(
            session,
            IngestRecord(
                project={
                    "company": "Acme",
                    "name": "North Campus",
                    "city": "Racine",
                    "state": "WI",
                },
                sources=[
                    SourceRecord(
                        url="https://news.example.com/north/",
                        source_type="company_filing",
                        fetched_at=T0,
                        excerpt="The campus will draw 900 MW.",
                        claims={
                            "name": "North Campus",
                            "company": "Acme",
                            "city": "Racine",
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
                        summary="Two upgrades outstanding.",
                        quote="must complete two 345-kilovolt upgrades",
                        source_url="https://news.example.com/north/",
                    )
                ],
            ),
        )
    return path


@pytest.fixture
def key(db):
    with session_scope(open_db(db, readonly=False)) as session:
        person = accounts.create(session, "reader@example.com", "a-long-password").id
        _row, made = apikeys.create(session, person, "test")
    return made


@pytest.fixture
def api(db):
    from http.server import ThreadingHTTPServer

    console = Console(db, api_host=API_HOST)
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


def call(address, path, key=None, host=None, method="GET", headers=None):
    conn = HTTPConnection(*address, timeout=30)
    head = dict(headers or {})
    if key:
        head["Authorization"] = f"Bearer {key}"
    if host:
        head["Host"] = host
    conn.request(method, path, body=b"{}" if method == "POST" else None, headers=head)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    conn.close()
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw
    return response.status, dict(response.getheaders()), body


# --- the way in -----------------------------------------------------------------------


def test_the_index_and_health_need_no_key(api):
    address, _ = api
    status, _, body = call(address, "/v1", host=API_HOST)
    assert status == 200 and body["version"] == "v1"
    assert "GET /v1/projects" in body["endpoints"]
    status, _, body = call(address, "/v1/health", host=API_HOST)
    assert status == 200 and body["status"] == "ok"


def test_everything_else_needs_a_key(api, key):
    address, _ = api
    for path in (
        "/v1/me",
        "/v1/projects",
        "/v1/projects/1",
        "/v1/capex",
        "/v1/updates",
        "/v1/conditions",
        "/v1/conditions/1",
    ):
        status, _, body = call(address, path, host=API_HOST)
        assert status == 401 and body["error"]["code"] == "key_required", path
    status, _, body = call(address, "/v1/projects", key=key[:-1] + "x", host=API_HOST)
    assert status == 401 and body["error"]["code"] == "key_invalid"


def test_a_console_session_is_not_a_key(api, db, key):
    """The cookie is never read: a page on another site cannot use a reader's session."""
    address, _ = api
    status, _, _ = call(
        address, "/api/v1/projects", headers={"Cookie": "dc_session=anything-at-all"}
    )
    assert status == 401


def test_a_revoked_key_or_a_disabled_account_stops_at_once(api, db, key):
    address, _ = api
    assert call(address, "/v1/me", key=key, host=API_HOST)[0] == 200
    with session_scope(open_db(db, readonly=False)) as session:
        accounts.by_email(session, "reader@example.com").disabled_at = T0
    assert call(address, "/v1/me", key=key, host=API_HOST)[0] == 401
    with session_scope(open_db(db, readonly=False)) as session:
        account = accounts.by_email(session, "reader@example.com")
        account.disabled_at = None
        row = apikeys.listed(session, account.id)[0]
        apikeys.revoke(session, account.id, row.id)
    assert call(address, "/v1/me", key=key, host=API_HOST)[0] == 401


def test_the_key_header_can_also_be_x_api_key(api, key):
    address, _ = api
    status, _, body = call(address, "/api/v1/me", headers={"X-API-Key": key})
    assert status == 200 and body["key"]["name"] == "test"
    assert key not in json.dumps(body), "the key is never echoed back"


def test_a_caller_that_loops_is_slowed(api, key):
    address, console = api
    console.api_limiter = publicapi.RateLimiter(per_minute=3)
    codes = [call(address, "/v1/me", key=key, host=API_HOST)[0] for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    status, headers, body = call(address, "/v1/me", key=key, host=API_HOST)
    assert status == 429 and int(headers["Retry-After"]) >= 1
    assert body["error"]["code"] == "rate_limited"


def test_the_limiter_refills_with_time():
    now = [0.0]
    limiter = publicapi.RateLimiter(per_minute=60, clock=lambda: now[0])
    for _ in range(60):
        assert limiter.take(1)[0]
    assert limiter.take(1)[0] is False
    now[0] += 1.0
    assert limiter.take(1)[0] is True, "one token a second at 60 a minute"
    assert limiter.take(2)[0] is True, "each key has its own bucket"


# --- the API hostname is only the API ---------------------------------------------


def test_nothing_of_the_console_answers_on_the_api_hostname(api, key):
    address, _ = api
    for path in ("/api/dataset", "/signin", "/static/app.js", "/docs/", "/projects"):
        status, _, body = call(address, path, key=key, host=API_HOST)
        assert status == 404, path
        assert body["error"]["code"] == "not_found"
    status, headers, _ = call(address, "/", host=API_HOST)
    assert status == 303 and headers["Location"] == "/v1"


def test_the_api_answers_no_writes(api, key):
    address, _ = api
    for path, host in (("/v1/projects", API_HOST), ("/api/login", API_HOST), ("/api/v1/me", None)):
        status, _, body = call(address, path, key=key, host=host, method="POST")
        assert status == 405 and body["error"]["code"] == "read_only", path


# --- the answers ----------------------------------------------------------------------


def test_projects_are_paged_and_filtered_like_the_table(api, key):
    address, _ = api
    status, _, body = call(address, "/v1/projects?state=WI&limit=5", key=key, host=API_HOST)
    assert status == 200 and body["total"] == 1 and body["next_offset"] is None
    row = body["data"][0]
    assert row["name"] == "North Campus" and row["mw_planned"] == 900.0
    assert row["links"]["self"] == f"/v1/projects/{row['id']}"
    assert call(address, "/v1/projects?state=TX", key=key, host=API_HOST)[2]["total"] == 0
    status, _, body = call(address, "/v1/projects?sort=nonsense", key=key, host=API_HOST)
    assert status == 400 and body["error"]["code"] == "bad_parameter"
    status, _, body = call(address, "/v1/projects?colour=red", key=key, host=API_HOST)
    assert status == 400 and body["error"]["code"] == "unknown_parameter"


def test_a_project_says_where_each_value_came_from(api, key):
    address, _ = api
    pid = call(address, "/v1/projects", key=key, host=API_HOST)[2]["data"][0]["id"]
    status, _, body = call(address, f"/v1/projects/{pid}", key=key, host=API_HOST)
    assert status == 200
    project = body["data"]
    planned = project["fields"]["mw_planned"]
    assert planned["value"] == 900.0 and planned["label"] == "quoted"
    assert "900 MW" in planned["quote"] and planned["source_url"].startswith("https://")
    assert project["fields"]["investment_usd"]["label"] == "missing"
    assert project["obstacles"][0]["category"] == "transmission"
    assert project["source_count"] == 1
    status, _, body = call(address, f"/v1/projects/{pid}/sources", key=key, host=API_HOST)
    assert status == 200 and body["data"][0]["url"] == "https://news.example.com/north/"


def test_a_campus_says_whether_it_is_a_suspected_delay(api, key):
    """Added to v1 rather than changed: a new key, a new filter, a new sort."""
    address, _ = api
    status, _, body = call(address, "/v1/projects?suspected_delay=true", key=key, host=API_HOST)
    assert status == 200 and body["total"] == 0, "the seeded campus has no date to miss"
    status, _, body = call(address, "/v1/projects?sort=days_late", key=key, host=API_HOST)
    assert status == 200 and body["data"][0]["suspected_delay"] is None
    pid = body["data"][0]["id"]
    status, _, body = call(address, f"/v1/projects/{pid}", key=key, host=API_HOST)
    assert status == 200 and body["data"]["suspected_delay"] is None
    status, _, body = call(address, "/v1/companies/acme", key=key, host=API_HOST)
    assert body["data"]["suspected_delays"] == {"count": 0, "mw_planned": 0, "project_ids": []}


def test_every_statement_behind_a_campus_and_its_company_record(api, key):
    """Appended to v1 (migrations 0043-0045): the facts route, the company record,
    and when a party's role ended."""
    address, _ = api
    pid = call(address, "/v1/projects", key=key, host=API_HOST)[2]["data"][0]["id"]
    status, _, body = call(address, f"/v1/projects/{pid}/facts", key=key, host=API_HOST)
    assert status == 200
    planned = [r for r in body["data"] if r["entity"] == "campus" and r["field"] == "mw_planned"]
    assert planned and planned[0]["number"] == 900.0 and planned[0]["held"] is True
    assert planned[0]["tier"] == "confirmed" and planned[0]["quote"] == "will draw 900 MW"
    assert "project_id" not in planned[0], "the path already names the campus"
    assert call(address, "/v1/projects/99999/facts", key=key, host=API_HOST)[0] == 404

    status, _, body = call(address, f"/v1/projects/{pid}", key=key, host=API_HOST)
    assert all("ended_on" in p and "ended_reason" in p for p in body["data"]["parties"])
    status, _, body = call(address, "/v1/companies/acme", key=key, host=API_HOST)
    company = body["data"]
    assert {"kind", "parent", "subsidiaries", "also_written"} <= set(company)
    assert company["parent"] is None and company["subsidiaries"] == []


def test_a_missing_project_says_it_may_have_been_merged(api, key):
    address, _ = api
    status, _, body = call(address, "/v1/projects/99999", key=key, host=API_HOST)
    assert status == 404 and "merged" in body["error"]["message"]
    assert call(address, "/v1/projects/abc", key=key, host=API_HOST)[0] == 400


def test_companies_capex_updates_and_me(api, key):
    address, _ = api
    status, _, body = call(address, "/v1/companies/acme", key=key, host=API_HOST)
    assert status == 200 and body["data"]["totals"]["projects"] == 1
    # Added to the shape, never renamed: companies newly named on its campuses.
    assert set(body["data"]["new_parties"]) == {"days", "since", "parties"}
    assert body["data"]["new_parties"]["days"] == 30
    assert call(address, "/v1/companies/Not_A_Slug", key=key, host=API_HOST)[0] == 400
    status, _, body = call(address, "/v1/capex", key=key, host=API_HOST)
    assert status == 200 and "buyers" in body and "floor" in body["note"]
    status, _, body = call(address, "/v1/updates?days=30", key=key, host=API_HOST)
    assert status == 200 and body["days"] == 30 and isinstance(body["data"], list)
    assert call(address, "/v1/updates?days=90", key=key, host=API_HOST)[0] == 400
    status, _, body = call(address, "/v1/me", key=key, host=API_HOST)
    assert body["account"]["email"] == "reader@example.com"
    assert body["key"]["last_used_at"] is not None, "using a key records it"


def test_an_unknown_endpoint_is_a_json_404(api, key):
    address, _ = api
    status, _, body = call(address, "/v1/nothing", key=key, host=API_HOST)
    assert status == 404 and body["error"]["code"] == "not_found"


# --- registering a key from the console ------------------------------------------------


def _signed_in(address, email="reader@example.com", password="a-long-password"):
    conn = HTTPConnection(*address, timeout=30)
    conn.request(
        "POST",
        "/api/login",
        body=json.dumps({"email": email, "password": password}),
        headers={"Content-Type": "application/json"},
    )
    response = conn.getresponse()
    response.read()
    cookie = response.getheader("Set-Cookie").split(";")[0]
    conn.close()
    return cookie


def _console(address, path, cookie, body=None):
    conn = HTTPConnection(*address, timeout=30)
    head = {"Cookie": cookie, "Content-Type": "application/json"}
    conn.request(
        "POST" if body is not None else "GET",
        path,
        body=json.dumps(body) if body is not None else None,
        headers=head,
    )
    response = conn.getresponse()
    data = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, data


def test_a_reader_makes_lists_uses_and_revokes_a_key(api, key):
    address, _ = api
    cookie = _signed_in(address)
    status, made = _console(address, "/api/account/keys/create", cookie, {"name": "laptop"})
    assert status == 200 and made["key"].startswith("dct_")
    new_key = made["key"]
    status, listed = _console(address, "/api/account/keys", cookie)
    assert status == 200 and {k["name"] for k in listed["keys"]} == {"test", "laptop"}
    assert new_key not in json.dumps(listed), "a list never carries a key"
    assert call(address, "/v1/me", key=new_key, host=API_HOST)[2]["key"]["name"] == "laptop"

    status, _ = _console(address, "/api/account/keys/revoke", cookie, {"id": made["api_key"]["id"]})
    assert status == 200
    assert call(address, "/v1/me", key=new_key, host=API_HOST)[0] == 401
    status, body = _console(address, "/api/account/keys/create", cookie, {"name": " "})
    assert status == 400 and "name" in body["error"]


def test_an_admin_switches_a_readers_api_access(api, db, key):
    """Off: the reader's key is refused with a reason, their card says so, and they
    cannot make another. On: the same key works again."""
    address, _ = api
    with session_scope(open_db(db, readonly=False)) as session:
        boss = accounts.create(session, "boss@example.com", "a-long-password")
        accounts.set_admin(session, boss, True)
        reader = accounts.by_email(session, "reader@example.com").id
    admin = _signed_in(address, "boss@example.com")
    status, body = _console(address, "/api/admin/users/update", admin, {"id": reader, "api": False})
    assert status == 200 and body["account"]["api"] is False
    assert any("API access off" in c for c in body["changes"])

    status, _, body = call(address, "/v1/me", key=key, host=API_HOST)
    assert status == 403 and body["error"]["code"] == "api_access_off"
    cookie = _signed_in(address)
    status, listed = _console(address, "/api/account/keys", cookie)
    assert status == 200 and listed["allowed"] is False and len(listed["keys"]) == 1
    status, body = _console(address, "/api/account/keys/create", cookie, {"name": "laptop"})
    assert status == 400 and "API access is off" in body["error"]

    status, body = _console(address, "/api/admin/users/update", admin, {"id": reader, "api": "no"})
    assert status == 400
    _console(address, "/api/admin/users/update", admin, {"id": reader, "api": True})
    assert call(address, "/v1/me", key=key, host=API_HOST)[0] == 200
    reader_session = _signed_in(address)
    status, _ = _console(
        address, "/api/admin/users/update", reader_session, {"id": reader, "api": True}
    )
    assert status == 403, "only an admin can switch it"


def test_key_routes_need_a_session(api, key):
    """With an account on the console the gate is closed; with none there is nobody
    a key could belong to."""
    address, _ = api
    assert _console(address, "/api/account/keys", "dc_session=nobody")[0] == 401


# --- rules that apply to a place ------------------------------------------------------
#
# Asked of the endpoint functions directly: the routes that reach them are the
# console's, in `server.Handler._api_answer`.

RULE_SENTENCES = {
    "rule": "The governor wants a statewide pause on new data center permits.",
    "status": "She pledged the pause during the campaign.",
}


def _rules(db) -> dict[str, int]:
    """Four rules, one reported a day apart: Wisconsin's pledge (reaching the North
    Campus), Piqua's ban, a Texas pause since lifted, and a transformer tariff."""
    from tracker import conditions

    base = {
        "title": "Wisconsin pause on new data center permits",
        "kind": "pledge",
        "channel": "permitting",
        "scope": "state",
        "state": "WI",
        "applies_to": "unbuilt",
        "status": "pledged",
        "summary": "The governor wants new data center permits paused.",
        "quotes": {"rule": "x"},
    }
    rows = {
        "wisconsin": (3, {}),
        "piqua": (2, {"title": "Piqua, OH ban on large data centers", "kind": "ban",
                      "scope": "place", "state": "OH", "place": "Piqua", "status": "passed",
                      "min_mw": 25}),
        "lifted": (1, {"title": "Texas pause on new data center permits", "state": "TX",
                       "status": "lifted"}),
        "tariff": (0, {"title": "Tariff on large power transformers", "kind": "tariff",
                       "channel": "equipment_supply", "scope": "input", "state": None,
                       "input": "large power transformers", "status": "in_force"}),
    }  # fmt: skip
    out: dict[str, int] = {}
    with session_scope(open_db(db, readonly=False)) as session:
        for name, (days, changes) in rows.items():
            [rule], bad = conditions.offered({"rules": [{**base, **changes}]})
            assert not bad, bad
            when = T0 - dt.timedelta(days=days)
            page = conditions.Page(f"https://news.example.com/{name}", name, when, "")
            condition, _ = conditions.attach(session, rule, RULE_SENTENCES, page, now=T0)
            out[name] = condition.id
        conditions.rebuild(session, now=T0)
    return out


def _ask(db, fn, *args):
    with session_scope(open_db(db), commit=False) as session:
        return fn(session, *args)


def _listed(db, **query):
    req = publicapi.Request(
        parts=(), query={k: [v] for k, v in query.items()}, caller=None, prefix="/v1"
    )
    return _ask(db, publicapi.conditions_list, req)


def test_rules_are_listed_newest_first_filtered_and_paged(db):
    ids = _rules(db)

    every = _listed(db)
    assert every["total"] == 4 and every["next_offset"] is None
    assert [r["id"] for r in every["data"]] == [
        ids["tariff"],
        ids["lifted"],
        ids["piqua"],
        ids["wisconsin"],
    ], "newest report first, a lifted rule included"

    [wisconsin] = _listed(db, state="wi")["data"]
    assert wisconsin["projects"] == 1 and wisconsin["where"] == "Wisconsin"
    assert wisconsin["effect"] == "restricts" and wisconsin["market_context"] is False
    assert wisconsin["links"] == {"self": f"/v1/conditions/{ids['wisconsin']}"}

    standing = _listed(db, status="pledged, passed")
    assert [r["id"] for r in standing["data"]] == [ids["piqua"], ids["wisconsin"]]
    [tariff] = _listed(db, scope="input")["data"]
    assert tariff["market_context"] is True and tariff["projects"] == 0

    page = _listed(db, limit="1", offset="1")
    assert (page["total"], page["limit"], page["next_offset"]) == (4, 1, 2)
    assert [r["id"] for r in page["data"]] == [ids["lifted"]]


def test_a_rule_summary_is_its_documented_shape_and_nothing_else(db):
    """A v1 shape is a contract: a key the CLI's JSON gains must not leak into it,
    and every key it has is in the reference, in both languages."""
    from pathlib import Path

    _rules(db)
    [row] = _listed(db, state="WI")["data"]
    documented = [*publicapi.CONDITION_FIELDS, "where", "market_context"]
    assert list(row) == [*documented, "links"]
    for lang in ("en", "zh"):
        page = (Path(publicapi.__file__).parent / "docs" / lang / "api-reference.md").read_text(
            encoding="utf-8"
        )
        section = page[page.index("## GET /v1/conditions") :]
        missing = [k for k in documented if f"`{k}`" not in section]
        assert not missing, f"the {lang} reference does not document {missing}"


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ({"sort": "id"}, "unknown_parameter"),
        ({"status": "rumoured"}, "bad_parameter"),
        ({"scope": "planet"}, "bad_parameter"),
        ({"state": "Texas"}, "bad_parameter"),
        ({"limit": "0"}, "bad_parameter"),
        ({"offset": "-1"}, "bad_parameter"),
        ({"limit": "many"}, "bad_parameter"),
    ],
)
def test_a_rule_filter_the_api_does_not_understand_is_a_400(db, query, code):
    with pytest.raises(publicapi.ApiError) as raised:
        _listed(db, **query)
    assert raised.value.status == 400 and raised.value.code == code


def test_a_rule_shows_its_sentences_and_each_campus_it_reaches_and_why(db):
    ids = _rules(db)
    detail = _ask(db, publicapi.condition_detail, str(ids["wisconsin"]), "/v1")

    assert detail["status"] == "pledged" and detail["projects"] == 1
    [article] = detail["articles"]
    assert article["url"] == "https://news.example.com/wisconsin"
    assert article["quotes"] == RULE_SENTENCES, "the article's own sentences, by what each says"
    [campus] = detail["affected_projects"]
    assert campus["name"] == "North Campus" and campus["state"] == "WI"
    assert campus["fit"] == "exact" and campus["why"].startswith("WI · construction")
    assert campus["links"] == {"self": f"/v1/projects/{campus['project_id']}"}

    lifted = _ask(db, publicapi.condition_detail, str(ids["lifted"]), "/v1")
    assert lifted["affected_projects"] == [] and lifted["projects"] == 0


def test_a_missing_or_malformed_rule_id_says_so(db):
    with pytest.raises(publicapi.ApiError) as raised:
        _ask(db, publicapi.condition_detail, "twelve", "/v1")
    assert (raised.value.status, raised.value.code) == (400, "bad_id")
    with pytest.raises(publicapi.ApiError) as raised:
        _ask(db, publicapi.condition_detail, "999", "/v1")
    assert raised.value.status == 404 and "dropped" in raised.value.message


def test_the_rule_endpoints_answer_over_http(api, db, key):
    ids = _rules(db)
    address, _ = api
    status, _, listed = call(address, "/v1/conditions?state=WI", key=key, host=API_HOST)
    assert status == 200 and [r["id"] for r in listed["data"]] == [ids["wisconsin"]]
    status, _, one = call(address, f"/v1/conditions/{ids['piqua']}", key=key, host=API_HOST)
    assert status == 200 and one["data"]["place"] == "Piqua"
    status, _, gone = call(address, "/v1/conditions/999", key=key, host=API_HOST)
    assert status == 404


def test_the_index_adds_the_rule_endpoints_and_keeps_every_other(api):
    address, _ = api
    _, _, body = call(address, "/v1", host=API_HOST)
    endpoints = set(body["endpoints"])
    assert {"GET /v1/conditions", "GET /v1/conditions/{id}"} <= endpoints
    assert {
        "GET /v1/health", "GET /v1/me", "GET /v1/projects", "GET /v1/projects/{id}",
        "GET /v1/projects/{id}/sources", "GET /v1/projects/{id}/facts",
        "GET /v1/companies/{slug}", "GET /v1/capex", "GET /v1/updates",
    } <= endpoints  # fmt: skip
