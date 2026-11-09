"""Rules that apply to a place: what is read, what is believed, what it reaches.

The load-bearing assertions are `test_a_statewide_rule_reaches_every_project_it_covers`
(the reason this exists: the Texas pledge reached 21 of 205 Texas projects, the ones a
story happened to name), `test_a_rule_whose_sentence_is_not_in_the_article_is_refused`
(the evidence rule) and `test_two_readings_of_one_rule_are_one_condition`.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import select

from tracker import conditions
from tracker.cli import app
from tracker.models import (
    Condition,
    ConditionRead,
    ConditionSource,
    Event,
    Exposure,
    IngestUrl,
    Project,
)

NOW = dt.datetime(2026, 11, 9, 12, 0)

TEXAS = (
    "Texas Governor-elect Gina Hinojosa said on Wednesday she wants a statewide pause on "
    "new data center permits until the legislature writes water and power rules. "
    "Hinojosa campaigned on freezing new data center construction across Texas. "
    "The governor's office said existing projects would not be affected."
)

PIQUA = (
    "In Piqua, Ohio, voters on Tuesday approved a ban on data centers over 25 megawatts. "
    "The charter amendment passed with 71% of the vote in the 2025 municipal cycle's "
    "successor election, and the city must now rewrite its zoning code."
)


def _rule(**kw) -> dict:
    base = {
        "title": "Texas pause on new data center permits",
        "kind": "pledge",
        "channel": "permitting",
        "scope": "state",
        "state": "TX",
        "applies_to": "new",
        "min_mw": None,
        "status": "pledged",
        "summary": "The governor-elect wants new data center permits paused.",
        "quotes": {
            "rule": "she wants a statewide pause on new data center permits until the "
            "legislature writes water and power rules",
            "status": "Hinojosa campaigned on freezing new data center construction across Texas.",
        },
        "same_as": None,
    }
    base.update(kw)
    return base


def _one(**kw) -> conditions.Rule:
    rules, bad = conditions.offered({"rules": [_rule(**kw)]})
    assert not bad, bad
    return rules[0]


def _page(url="https://news.example/tx", text=TEXAS, title="Texas governor-elect", days=0):
    return conditions.Page(url, title, NOW - dt.timedelta(days=days), text)


def _project(session, *, state="TX", city="Abilene", county="Taylor", phase="announced", mw=None):
    n = session.query(Project).count() + 1
    project = Project(
        name=f"Campus {n}",
        company="Acme",
        city=city,
        county=county,
        state=state,
        dedup_key=f"acme|{n}|{state}",
        phase=phase,
        mw_planned=mw,
        confidence=1,
    )
    session.add(project)
    session.flush()
    return project


# --- which pages are worth a call ---------------------------------------------


def test_a_page_stating_a_rule_is_read_and_a_campus_story_is_not():
    assert conditions.worth_reading(TEXAS + " The data center bill and the ballot measure.")
    campus = (
        "Acme broke ground on its 300 MW data center campus in Abilene on Monday, "
        "with the first building due in 2027."
    )
    assert not conditions.worth_reading(campus)


# --- what is believed ---------------------------------------------------------


def test_an_offered_rule_is_normalised_and_a_malformed_one_says_why():
    rules, bad = conditions.offered(
        {
            "rules": [
                _rule(state="Texas", status="In Force", kind="Executive Order"),
                _rule(title="no place", scope="county", county=None),
                _rule(title="no such status", status="rumoured"),
            ]
        }
    )
    assert [r.state for r in rules] == ["TX"]
    assert rules[0].status == "in_force" and rules[0].kind == "executive_order"
    assert rules[0].effect == "restricts", "an unclassed rule is taken to restrict"
    assert [t for t, _ in bad] == ["no place", "no such status"]


def test_a_rule_whose_sentence_is_not_in_the_article_is_refused():
    rule = _one(quotes={"rule": "Texas banned every new data center outright this week."})
    kept, refusal = conditions.gate(rule, TEXAS)
    assert not kept and "not in the article" in refusal


def test_the_articles_own_sentence_is_kept_not_the_models():
    rule = _one()
    kept, refusal = conditions.gate(rule, TEXAS)
    assert not refusal
    assert kept["rule"] in TEXAS and kept["rule"].endswith("power rules.")


def test_a_status_the_sentence_does_not_bear_out_is_refused():
    rule = _one(
        status="in_force",
        quotes={
            "rule": TEXAS.split(". ")[0],
            "status": "The governor's office said existing projects would not be affected.",
        },
    )
    _, refusal = conditions.gate(rule, TEXAS)
    assert "does not say it is in force" in refusal


def test_the_place_must_be_named_in_the_sentences_or_the_headline():
    text = "She wants a pause on new data center permits until rules are written."
    rule = _one(quotes={"rule": text, "status": text})
    _, refusal = conditions.gate(rule, text, title="Governor-elect's first week")
    assert "never names Texas" in refusal
    kept, refusal = conditions.gate(rule, text, title="Texas governor-elect's first week")
    assert kept and not refusal
    # Named elsewhere in the article: that sentence is kept as the place's.
    longer = "Voters in Texas elected a new governor on Tuesday. " + text
    kept, refusal = conditions.gate(rule, longer, title="Governor-elect's first week")
    assert not refusal and kept["place"] == "Voters in Texas elected a new governor on Tuesday."


def test_a_threshold_must_be_stated_and_a_year_does_not_count():
    rule = _one(
        title="Piqua ban",
        kind="ban",
        scope="place",
        state="OH",
        place="Piqua",
        status="passed",
        min_mw=25,
        quotes={"rule": PIQUA.split(". ")[0] + "."},
    )
    kept, refusal = conditions.gate(rule, PIQUA)
    assert kept and not refusal
    # "2025" in the article does not state a 20 MW threshold, nor does "25".
    no_number = _one(
        scope="place", state="OH", place="Piqua", status="passed", min_mw=20,
        quotes={"rule": PIQUA.split(". ")[0] + ".", "status": PIQUA.split(". ")[1] + "."},
    )  # fmt: skip
    _, refusal = conditions.gate(no_number, PIQUA)
    assert "20 MW threshold" in refusal


# --- one rule, however many articles ------------------------------------------


def test_two_readings_of_one_rule_are_one_condition(session):
    first, _ = conditions.attach(session, _one(), {"rule": "a"}, _page(days=2), now=NOW)
    second, new = conditions.attach(
        session, _one(title="Texas pause on data center permits", summary="Later."),
        {"rule": "b"}, _page(url="https://news.example/tx2"), now=NOW,
    )  # fmt: skip
    assert second.id == first.id and not new
    assert len(second.sources) == 2
    assert second.summary == "Later."


def test_two_rules_of_one_kind_in_one_town_stay_two(session):
    """Aurora, Colorado had a residential buffer and an airport district's by-right
    zoning in one week: same town, same kind, same need, different rules."""
    buffer, _ = conditions.attach(
        session,
        _one(title="Aurora, CO residential buffer for data centers", kind="zoning",
             scope="place", state="CO", place="Aurora", status="proposed"),
        {"rule": "a"}, _page(url="https://news.example/a1"), now=NOW,
    )  # fmt: skip
    airport, new = conditions.attach(
        session,
        _one(title="Aurora, CO Airport District use-by-right zoning", kind="zoning",
             scope="place", state="CO", place="Aurora", status="in_force", effect="eases"),
        {"rule": "b"}, _page(url="https://news.example/a2"), now=NOW,
    )  # fmt: skip
    assert new and airport.id != buffer.id
    assert airport.effect == "eases" and buffer.effect == "restricts"


def test_two_titles_of_one_moratorium_in_one_town_are_one_rule(session):
    """West Richland, WA, 2026-11-09: "interim moratorium" and "six-month moratorium on
    data center development" were filed as two rules; the town's name is not evidence
    either way, and is left out of the comparison."""
    town = {"kind": "moratorium", "scope": "place", "state": "WA", "place": "West Richland",
            "status": "proposed"}  # fmt: skip
    first, _ = conditions.attach(
        session, _one(title="West Richland, WA interim moratorium on data centers", **town),
        {"rule": "a"}, _page(url="https://news.example/w1"), now=NOW,
    )  # fmt: skip
    second, new = conditions.attach(
        session,
        _one(title="West Richland, WA six-month moratorium on data center development", **town),
        {"rule": "b"}, _page(url="https://news.example/w2"), now=NOW,
    )  # fmt: skip
    assert not new and second.id == first.id


def test_the_newest_article_sets_the_status(session):
    pledge, _ = conditions.attach(session, _one(), {"rule": "a"}, _page(days=5), now=NOW)
    order, _ = conditions.attach(
        session,
        _one(kind="pledge", status="in_force", same_as=pledge.id),
        {"rule": "b"},
        _page(url="https://news.example/order", days=1),
        now=NOW,
    )
    assert order.id == pledge.id and order.status == "in_force"
    # An older article read later does not set it back.
    late, _ = conditions.attach(
        session, _one(status="proposed"), {"rule": "c"},
        _page(url="https://news.example/old", days=30), now=NOW,
    )  # fmt: skip
    assert late.id == pledge.id and late.status == "in_force"
    assert late.first_reported == (NOW - dt.timedelta(days=30)).date()


def test_same_as_is_ignored_when_it_names_another_place(session):
    texas, _ = conditions.attach(session, _one(), {"rule": "a"}, _page(), now=NOW)
    ohio, new = conditions.attach(
        session,
        _one(title="Ohio pause", state="OH", same_as=texas.id),
        {"rule": "b"},
        _page(url="https://news.example/oh"),
        now=NOW,
    )
    assert new and ohio.id != texas.id


# --- which projects a rule reaches --------------------------------------------


def test_a_statewide_rule_reaches_every_project_it_covers(session):
    """The Texas pledge reached 21 of 205 Texas projects through articles that named
    them. A rule for new permits reaches every Texas project without one, and no
    project already permitted, running, or elsewhere."""
    waiting = [_project(session) for _ in range(3)]
    permitted = _project(session, phase="permitting")
    session.add(Event(project_id=permitted.id, event_type="permit_approved",
                      event_date=dt.date(2026, 9, 1), description="Approved."))  # fmt: skip
    running = _project(session, phase="operational")
    elsewhere = _project(session, state="OK", city="Tulsa", county="Tulsa")
    conditions.attach(session, _one(), {"rule": "a"}, _page(), now=NOW)

    got = conditions.rebuild(session, now=NOW)

    reached = set(session.scalars(select(Exposure.project_id)))
    assert reached == {p.id for p in waiting}
    assert not reached & {permitted.id, running.id, elsewhere.id}
    assert got.added == 3
    assert all(e.fit == "exact" for e in session.scalars(select(Exposure)))
    assert "no permit on record" in session.scalars(select(Exposure.why)).first()


def test_a_town_rule_is_likely_and_a_size_threshold_is_checked(session):
    big = _project(session, state="OH", city="Piqua", county="Miami", mw=300)
    small = _project(session, state="OH", city="Piqua", county="Miami", mw=10)
    unknown = _project(session, state="OH", city="Piqua", county="Miami")
    other_town = _project(session, state="OH", city="Troy", county="Miami", mw=300)
    conditions.attach(
        session,
        _one(title="Piqua ban", kind="ban", scope="place", state="OH", place="Piqua",
             status="passed", min_mw=25, applies_to="unbuilt"),
        {"rule": "a"}, _page(url="https://news.example/piqua", text=PIQUA), now=NOW,
    )  # fmt: skip
    conditions.rebuild(session, now=NOW)

    fits = {e.project_id: (e.fit, e.why) for e in session.scalars(select(Exposure))}
    assert set(fits) == {big.id, unknown.id}
    assert fits[big.id][0] == "likely" and "town limits not checked" in fits[big.id][1]
    assert "size unknown" in fits[unknown.id][1]
    assert small.id not in fits and other_town.id not in fits


def test_a_grid_region_rule_reaches_only_states_mostly_in_that_region(session):
    texas = _project(session)
    illinois = _project(session, state="IL", city="Chicago", county="Cook")
    conditions.attach(
        session,
        _one(title="ERCOT large-load rule", kind="grid_rule", channel="grid_capacity",
             scope="grid_region", state=None, region="Electric Reliability Council of Texas",
             status="in_force", applies_to="unbuilt"),
        {"rule": "a"}, _page(url="https://news.example/ercot"), now=NOW,
    )  # fmt: skip
    conditions.rebuild(session, now=NOW)
    fits = {e.project_id: e.fit for e in session.scalars(select(Exposure))}
    assert fits == {texas.id: "likely"}
    assert illinois.id not in fits


def test_a_rule_lifted_or_for_the_whole_industry_reaches_nobody(session):
    project = _project(session)
    texas, _ = conditions.attach(session, _one(), {"rule": "a"}, _page(days=3), now=NOW)
    conditions.attach(
        session,
        _one(title="Transformer tariff", kind="tariff", channel="equipment_supply",
             scope="input", state=None, input="large power transformers", status="in_force"),
        {"rule": "a"}, _page(url="https://news.example/tariff"), now=NOW,
    )  # fmt: skip
    conditions.rebuild(session, now=NOW)
    assert set(session.scalars(select(Exposure.project_id))) == {project.id}
    assert [c.title for c in conditions.market(session)] == ["Transformer tariff"]

    conditions.attach(
        session, _one(status="lifted", quotes={"rule": "x"}), {"rule": "x"},
        _page(url="https://news.example/lifted"), now=NOW,
    )  # fmt: skip
    got = conditions.rebuild(session, now=NOW)
    assert texas.status == "lifted"
    assert got.dropped == 1 and not session.scalars(select(Exposure)).all()


def test_first_seen_survives_a_rebuild(session):
    _project(session)
    conditions.attach(session, _one(), {"rule": "a"}, _page(), now=NOW)
    conditions.rebuild(session, now=NOW)
    later = NOW + dt.timedelta(days=3)
    got = conditions.rebuild(session, now=later)
    assert got.kept == 1 and got.added == 0
    assert session.scalars(select(Exposure.first_seen)).one() == NOW


# --- reading, end to end ------------------------------------------------------


class _Model:
    def __init__(self, *replies: dict):
        self.replies = [json.dumps(r) for r in replies]
        self.prompts: list[str] = []

    def complete(self, *, system, user, max_tokens=None):
        self.prompts.append(user)
        text = self.replies.pop(0) if self.replies else '{"rules": []}'

        class R:
            pass

        r = R()
        r.text, r.model, r.prompt_tokens, r.completion_tokens = text, "test", 10, 10
        return r


def _saved(session, cache: Path, url: str, text: str, *, status="no_project") -> None:
    from tracker.ingest.fetch import cache_path

    path = cache_path(url, cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    session.add(
        IngestUrl(url=url, run_id="t", status=status, title="Texas governor-elect",
                  last_tried_at=NOW, first_seen_at=NOW)
    )  # fmt: skip
    session.flush()


def test_a_saved_page_is_read_once_and_its_rule_kept(session, tmp_path):
    cache = tmp_path / "articles"
    body = TEXAS + " The data center bill, the ballot and the moratorium."
    _saved(session, cache, "https://news.example/tx", body)
    _project(session)
    model = _Model({"rules": [_rule()]})

    items, looked = conditions.pages(session, since=NOW - dt.timedelta(days=1), cache_dir=cache)
    assert [p.url for p in items] == ["https://news.example/tx"] and looked == 1
    run = conditions.read(session, items, extractor=model, now=NOW)

    [result] = run.results
    assert result.kept and result.kept[0][3] is True
    held = session.scalars(select(Condition)).one()
    assert held.status == "pledged" and held.state == "TX"
    assert session.scalars(select(ConditionSource)).one().url == "https://news.example/tx"
    assert session.get(ConditionRead, "https://news.example/tx").found == 1

    again, _ = conditions.pages(session, since=NOW - dt.timedelta(days=1), cache_dir=cache)
    assert again == [], "a page read under this prompt is not paid for again"


def test_until_bounds_the_pages_to_a_past_day(session, tmp_path):
    cache = tmp_path / "articles"
    body = TEXAS + " The data center bill, the ballot and the moratorium."
    _saved(session, cache, "https://news.example/tx", body)
    since = NOW - dt.timedelta(days=1)
    assert conditions.pages(session, since=since, until=NOW, cache_dir=cache)[0] == []
    later = NOW + dt.timedelta(hours=1)
    assert len(conditions.pages(session, since=since, until=later, cache_dir=cache)[0]) == 1


def test_the_model_is_shown_the_rules_held_for_the_states_named(session, tmp_path):
    held, _ = conditions.attach(session, _one(), {"rule": "a"}, _page(), now=NOW)
    model = _Model({"rules": []})
    conditions.read(session, [_page(url="https://news.example/x")], extractor=model, now=NOW)
    assert f"#{held.id} [TX, state, permitting, pledged, restricts]" in model.prompts[0]


def test_a_refused_rule_is_counted_and_not_stored(session):
    model = _Model({"rules": [_rule(quotes={"rule": "Texas outlawed data centers."})]})
    run = conditions.read(session, [_page()], extractor=model, now=NOW)
    [result] = run.results
    assert not result.kept and result.refused
    assert session.scalars(select(Condition)).all() == []
    assert session.get(ConditionRead, "https://news.example/tx").refused == 1


def test_a_dry_run_writes_nothing(session):
    model = _Model({"rules": [_rule()]})
    run = conditions.read(session, [_page()], extractor=model, commit=False, now=NOW)
    assert run.results[0].kept
    assert session.scalars(select(Condition)).all() == []
    assert session.scalars(select(ConditionRead)).all() == []


# --- the command --------------------------------------------------------------


def test_the_listing_and_show_read_the_held_rules(engine, db_path):
    from typer.testing import CliRunner

    from tracker.db import session_scope

    with session_scope(engine) as session:
        _project(session)
        conditions.attach(session, _one(), {"rule": "a"}, _page(), now=NOW)
        conditions.rebuild(session, now=NOW)
    runner = CliRunner()
    listed = runner.invoke(app, ["--db", str(db_path), "--json", "conditions"])
    assert listed.exit_code == 0, listed.output
    [row] = json.loads(listed.stdout)
    assert row["title"] == "Texas pause on new data center permits" and row["projects"] == 1
    shown = runner.invoke(
        app, ["--db", str(db_path), "--json", "conditions", "show", str(row["id"])]
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["exposures"][0]["fit"] == "exact"
