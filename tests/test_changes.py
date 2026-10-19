"""`tracker changes` and `tracker logic rule-out`: reading what a night changed, and
taking back a claim a person finds wrong.

Both came out of reading the night of 2026-09-30 by hand. The quality counts held
steady while four values went wrong, each with a verbatim sentence behind it — wrong
about what the sentence was about, which only a reader catches. One command puts each
change beside its sentence; the other undoes one in the way the audit's own repair
does, so the row is re-derived rather than typed into.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracker.cli import app
from tracker.db import init_db, session_scope
from tracker.models import Project

runner = CliRunner()
NOW = dt.datetime(2026, 9, 30, 12, 0, 0)


def _cite(url: str, value: float, quote: str, when: dt.datetime = NOW):
    from tracker.ingest.records import SourceRecord

    return SourceRecord(
        url=url,
        source_type="trade_press",
        excerpt="e",
        claims={"investment_usd": value},
        quotes={"investment_usd": quote},
        fetched_at=when,
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A database holding one campus with two quoted investment figures."""
    from tracker.ingest.records import IngestRecord
    from tracker.upsert import upsert_record

    path = tmp_path / "tracker.db"
    engine, _ = init_db(path)
    with session_scope(engine) as s:
        upsert_record(
            s,
            IngestRecord(
                project={
                    "company": "Google",
                    "name": "New Albany",
                    "city": "New Albany",
                    "state": "OH",
                },
                sources=[
                    _cite(
                        "https://a.test/campus",
                        600_000_000,
                        "Google to go ahead with $600 million data center in New Albany",
                    ),
                    _cite(
                        "https://b.test/state",
                        20_000_000_000,
                        "The company has invested $20 billion+ in Ohio since 2019",
                        NOW + dt.timedelta(days=30),
                    ),
                ],
            ),
        )
    engine.dispose()
    return path


def _snapshot(path: Path, to: Path) -> Path:
    con = sqlite3.connect(path)
    try:
        con.execute("VACUUM INTO ?", (str(to),))
    finally:
        con.close()
    return to


def _row(path: Path) -> Project:
    engine, _ = init_db(path)
    with session_scope(engine, commit=False) as s:
        row = s.query(Project).one()
        s.expunge(row)
    engine.dispose()
    return row


def test_rule_out_takes_one_claim_back_and_the_row_rederives(db):
    """The repair the audit itself makes: mark the claim, empty the field, re-derive."""
    held = _row(db).investment_usd
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "logic",
            "rule-out",
            str(_row(db).id),
            "investment_usd",
            "--citation",
            "https://b.test/state" if held == 20_000_000_000 else "https://a.test/campus",
            "--why",
            "a statewide total, not this campus's",
        ],
    )
    assert result.exit_code == 0, result.output
    row = _row(db)
    assert row.investment_usd != held, "the other quoted figure now decides the field"
    assert "operator resolved `operator_ruled_out`" in row.notes
    assert "a statewide total" in row.notes


def test_rule_out_names_the_citations_it_could_have_meant(db):
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "logic",
            "rule-out",
            str(_row(db).id),
            "investment_usd",
            "--citation",
            "https://nowhere.test/x",
            "--why",
            "w",
        ],
    )
    assert result.exit_code == 2
    assert "https://a.test/campus" in result.output and "https://b.test/state" in result.output


def test_a_dry_run_writes_nothing(db):
    before = _row(db).investment_usd
    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "logic",
            "rule-out",
            str(_row(db).id),
            "investment_usd",
            "--citation",
            "https://a.test/campus",
            "--citation",
            "https://b.test/state",
            "--why",
            "w",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    assert _row(db).investment_usd == before


def test_changes_lists_each_new_value_beside_its_sentence(db, tmp_path):
    """The counts cannot see a wrong value that has a quote; a reader can, given both."""
    snapshot = _snapshot(db, tmp_path / "before.db")
    row = _row(db)
    held = row.investment_usd
    loser = "https://b.test/state" if held == 20_000_000_000 else "https://a.test/campus"
    runner.invoke(
        app,
        [
            "--db",
            str(db),
            "logic",
            "rule-out",
            str(row.id),
            "investment_usd",
            "--citation",
            loser,
            "--why",
            "test",
        ],
    )

    result = runner.invoke(app, ["--db", str(db), "changes", "--against", str(snapshot)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "investment_usd:" in out and "->" in out
    assert "behind it:" in out, "the sentence now behind the new value is shown"
    assert "operator_ruled_out" in out, "and the decision that made the change"


def test_a_reused_id_is_a_new_row_not_a_changed_one(db, tmp_path):
    """Before migration 0032 SQLite handed a merged-away row's id to the next insert,
    as #1557 was on 2026-09-30, and the backups from then hold such ids. Read by id
    alone, Skybox would have looked like Nebius edited. Since 0032 an id is never
    reissued, so the reuse is made explicitly here."""
    snapshot = _snapshot(db, tmp_path / "before.db")
    engine, _ = init_db(db)
    with session_scope(engine) as s:
        old = s.query(Project).one()
        reused = old.id
        s.delete(old)
        s.flush()
        s.add(
            Project(
                id=reused,
                company="Skybox",
                name="Austin",
                city="Austin",
                state="TX",
                dedup_key="skybox|city:austin|TX",
            )
        )
    with session_scope(engine, commit=False) as s:
        from tracker import changes

        found = changes.diff(snapshot, s)
    engine.dispose()
    assert [pid for pid, _ in found.added] == [reused]
    assert [pid for pid, _ in found.removed] == [reused]
    assert found.values == []


def test_an_untouched_database_has_nothing_to_report(db, tmp_path):
    snapshot = _snapshot(db, tmp_path / "before.db")
    result = runner.invoke(app, ["--db", str(db), "changes", "--against", str(snapshot)])
    assert result.exit_code == 0, result.output
    assert "nothing" in result.output


def test_derived_values_are_traced_rather_than_reported_unbacked():
    """2026-10-13 printed "NOTHING" against a county the merge spelled out and two
    customers copied up from tranches — three values that each had a source."""
    import json
    from types import SimpleNamespace as NS

    from tracker.changes import backing

    source = NS(
        id=7,
        url="https://mississippi.example/aws",
        claims=json.dumps({"county": "Madison"}),
        quotes=None,
        unconfirmed_reasons=None,
    )
    tranche = NS(
        customer="BluSky AI",
        label="BluSky AI",
        source_id=7,
        quotes=json.dumps({"customer": "BluSky AI signs letter of intent"}),
    )
    row = NS(county="Madison County", customer="BluSky AI", sources=[source], blocks=[tranche])

    county = backing(row, "county")
    assert (county.url, county.quote) == ("https://mississippi.example/aws", "")
    customer = backing(row, "customer")
    assert (customer.url, customer.quote) == (
        "https://mississippi.example/aws",
        "BluSky AI signs letter of intent",
    )
    assert customer.via == "tranche 'BluSky AI'"
    row.customer = "Someone Else"
    assert not backing(row, "customer").url, "a tranche naming another tenant is not backing"


def _stored(**fields):
    """A project as `backing` reads it: the value, and what might stand behind it."""
    from types import SimpleNamespace as NS

    base = {"sources": [], "risks": [], "blocks": [], "parties": []}
    return NS(**{**base, **fields})


def _citation(id_: int, url: str, claims: dict, quotes=None, reasons=None, **more):
    import json
    from types import SimpleNamespace as NS

    return NS(
        id=id_,
        url=url,
        claims=json.dumps(claims),
        quotes=json.dumps(quotes or {}),
        unconfirmed_reasons=json.dumps(reasons or {}),
        **more,
    )


def test_a_blocker_is_traced_to_the_obstacle_it_was_derived_from():
    """2026-10-19 printed "NOTHING" against #183's blocker and "no quote" against
    fifteen others: `blocker` is the summary of an open obstacle, the obstacle carries
    the sentence, and no citation has ever held a quote for it — of 817 citations
    claiming a blocker that night, none did. #183's had been confirmed that evening."""
    from types import SimpleNamespace as NS

    from tracker.changes import backing

    summary = "Oracle's credit rating slipped to BBB-."
    row = _stored(
        blocker=summary,
        # A citation whose `blocker` claim is some other line from the same article,
        # unquoted — what the old report fell back on, or found nothing in.
        sources=[
            _citation(6582, "https://startup.example/cracks", {"blocker": "Permits delayed it."})
        ],
        risks=[
            NS(
                id=1481,
                status="open",
                summary=summary,
                quote="About $18 billion in loans tied to the project are trading at 89 cents.",
                unconfirmed=None,
                source_id=6582,
            ),
            # Resolved obstacles do not set the blocker, whatever they say.
            NS(id=24, status="resolved", summary=summary, quote="x", unconfirmed=None, source_id=1),
        ],
    )

    got = backing(row, "blocker")
    assert got.url == "https://startup.example/cracks"
    assert got.quote.startswith("About $18 billion")
    assert got.via == "obstacle 1481"


def test_an_unquoted_obstacle_says_why_rather_than_nothing():
    from types import SimpleNamespace as NS

    from tracker.changes import backing

    row = _stored(
        blocker="Cost recovery is unresolved.",
        sources=[_citation(9, "https://utility.example/pike", {"mw_planned": 1})],
        risks=[
            NS(
                id=1464,
                status="open",
                summary="Cost recovery is unresolved.",
                quote=None,
                unconfirmed="quote_off_target",
                source_id=9,
            )
        ],
    )
    got = backing(row, "blocker")
    assert (got.url, got.quote, got.via) == ("https://utility.example/pike", "", "obstacle 1464")
    assert got.why == "the one offered does not state it"


def test_a_capacity_the_tranches_filled_is_traced_to_their_sentences():
    """#1608's 3 MW built was `blocks.reconcile` filling the row from its one quoted
    tranche, and the report said nothing stood behind it."""
    import json
    from types import SimpleNamespace as NS

    from tracker.changes import backing

    def tranche(key, label, mw, status, quote=None, source_id=1):
        return NS(
            block_key=key,
            label=label,
            mw=mw,
            status=status,
            customer=None,
            generic=False,
            parent=None,
            quotes=json.dumps({"mw": quote} if quote else {}),
            unconfirmed_fields=None,
            source_id=source_id,
        )

    release = _citation(1, "https://databank.example/kc3", {"mw_planned": 8}, {"mw_planned": "8MW"})
    one = _stored(
        mw_built=3.0,
        sources=[release],
        blocks=[tranche("phase-1", "Phase I", 3.0, "serving", "3MW of power capacity in Phase I")],
    )
    got = backing(one, "mw_built")
    assert (got.url, got.quote) == (
        "https://databank.example/kc3",
        "3MW of power capacity in Phase I",
    )
    assert got.via == "tranche 'Phase I'"

    two = _stored(
        mw_planned=300.0,
        sources=[release],
        blocks=[
            tranche("building-a", "Building A", 100.0, "planned"),
            tranche("building-b", "Building B", 200.0, "planned", "Building B is 200 MW"),
        ],
    )
    got = backing(two, "mw_planned")
    assert got.quote == "Building B is 200 MW", "the sentence any of them carries"
    assert got.via.startswith("the sum of 2 tranches: 'Building A' 100 MW, 'Building B' 200 MW")

    two.mw_planned = 250.0
    assert not backing(two, "mw_planned").via, "a figure the tranches do not add up to"


def test_a_census_lookup_is_shown_as_a_derivation_not_as_a_missing_quote():
    from tracker.changes import backing

    census = _citation(
        3,
        "https://www2.census.gov/geo/docs/reference/codes2020/national_place_by_county2020.txt",
        {"county": "Butler County"},
        extractor="derived:census-place-2020",
        excerpt="Census 2020 place-to-county: Trenton, OH is in Butler County.",
    )
    got = backing(_stored(county="Butler County", sources=[census]), "county")
    assert got.derived and got.via == "derived from reference data"
    assert got.why == "Census 2020 place-to-county: Trenton, OH is in Butler County."


def test_a_customer_named_by_a_party_and_a_phase_set_by_named_tranches_are_traced():
    import json
    from types import SimpleNamespace as NS

    from tracker.changes import backing

    party = NS(
        name="Oracle", role="customer", unconfirmed=None, quote="leased to Oracle", source_id=4
    )
    lease = _citation(4, "https://lease.example/abilene", {"mw_planned": 1})
    got = backing(_stored(customer="Oracle", sources=[lease], parties=[party]), "customer")
    assert (got.url, got.quote, got.via) == (
        "https://lease.example/abilene",
        "leased to Oracle",
        "named as the customer",
    )

    hall = NS(label="Building 1", status="serving", generic=False, quotes=json.dumps({}))
    got = backing(_stored(phase="operational", blocks=[hall]), "phase")
    assert got.derived and got.via == "its tranches' status"
    assert got.why == "'Building 1' (serving)"


def test_the_report_says_no_quote_only_when_it_is_true_and_nothing_only_when_nothing():
    from tracker.changes import Changes, ValueChange, render

    found = Changes(
        values=[
            ValueChange(
                1, "A", "blocker", None, "x", "https://a.example", "Line one.\n\nLine two."
            ),
            ValueChange(
                2,
                "B",
                "blocker",
                None,
                "y",
                "https://b.example",
                "",
                "obstacle 7",
                "none was offered",
            ),
            ValueChange(
                3,
                "C",
                "county",
                None,
                "Z County",
                "https://census.example",
                "",
                "derived from reference data",
                "Census: X is in Z County.",
                True,
            ),
            ValueChange(4, "D", "mw_built", None, 5),
        ]
    )
    behind = [line.strip() for line in render(found) if "behind it" in line]
    assert behind[0] == 'behind it: "Line one. Line two." — https://a.example', "one line"
    assert behind[1] == "behind it: no quote (none was offered) — https://b.example (obstacle 7)"
    assert behind[2] == (
        "behind it: derived from reference data, not a sentence — Census: X is in Z County. "
        "— https://census.example"
    )
    assert behind[3].startswith("behind it: NOTHING")


def test_the_morning_report_traces_an_obstacle_and_a_tranche_end_to_end(db, tmp_path):
    """Through the write path rather than hand-built rows: the obstacle's sentence and
    the tranche's sentence are what the night's changes are printed beside."""
    from tracker.ingest.records import BlockRecord, IngestRecord, RiskRecord, SourceRecord
    from tracker.upsert import upsert_record

    snapshot = _snapshot(db, tmp_path / "before.db")
    engine, _ = init_db(db)
    with session_scope(engine) as s:
        upsert_record(
            s,
            IngestRecord(
                project={
                    "company": "Google",
                    "name": "New Albany",
                    "city": "New Albany",
                    "state": "OH",
                },
                sources=[
                    SourceRecord(
                        url="https://c.test/phase-one",
                        source_type="trade_press",
                        excerpt="e",
                        claims={"blocker": "Neighbours object to the noise."},
                        fetched_at=NOW,
                        blocks=[
                            BlockRecord(
                                label="Phase I",
                                mw=3.0,
                                status="serving",
                                quotes={"mw": "Phase I brings 3MW of power capacity."},
                            )
                        ],
                    )
                ],
                risks=[
                    RiskRecord(
                        category="community_opposition",
                        severity="watch",
                        summary="Neighbours object to the noise.",
                        quote="Residents told the council the fans run all night.",
                        source_url="https://c.test/phase-one",
                    )
                ],
            ),
        )
    engine.dispose()

    result = runner.invoke(app, ["--db", str(db), "changes", "--against", str(snapshot)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert '"Residents told the council the fans run all night."' in out
    assert '"Phase I brings 3MW of power capacity."' in out and "(tranche 'Phase I')" in out
    assert "NOTHING" not in out and "no quote" not in out


def test_a_noted_ruling_that_left_the_value_as_it_was_is_marked():
    """A ruling that struck the citation stating a value while another states it too
    is still a decision, and is listed — marked, so it is not read as a change."""
    from tracker.changes import Changes, render

    found = Changes(
        decisions=[
            (
                143,
                "IREN",
                "2026-10-19 agent (0.83) resolved `built_exceeds_planned`: "
                "mw_built 750 -> 750 (1 claim(s) superseded on citation(s) [1597])",
            ),
            (
                81,
                "Meta",
                "2026-10-19 agent (0.78) resolved `built_exceeds_planned`: "
                "mw_planned 33.185 -> 200 (1 claim(s) superseded on citation(s) [3139])",
            ),
        ]
    )
    lines = render(found)
    assert lines[0].endswith("2 decision(s) noted, 1 of them leaving the value as it was")
    assert "#143 IREN: (value unchanged) 2026-10-19" in lines[1]
    assert "(value unchanged)" not in lines[2]
