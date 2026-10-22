"""Every statement every source makes, in one table (migrations 0045, 0048).

Step 4.5 of the client plan (2026-10-21, A4). A cache rebuilt from the sources, so
these pin what a cache owes: it says what the sources say, classifies each statement
the way the rest of the tracker does, marks the one the row holds, and a second
rebuild changes nothing.

And, since the export, the API and the project page read it instead of each
citation's JSON, two more: read from the table, every surface says exactly what it
said read from the JSON (the fleet below), and no write — whichever path made it —
leaves the table describing sources that are no longer there (the sequence below).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tracker import facts
from tracker.ingest.records import (
    BlockRecord,
    EventRecord,
    IngestRecord,
    PartyRecord,
    RiskRecord,
    SourceRecord,
)
from tracker.models import Event, Fact, Project, Risk, Source


def _campus(session) -> Project:
    project = Project(
        name="Abilene",
        company="Crusoe",
        state="TX",
        city="Abilene",
        dedup_key="crusoe|city:abilene|TX",
        phase="construction",
        confidence=2,
        mw_planned=1200.0,
    )
    session.add(project)
    session.flush()
    return project


def _source(session, project, url, **kwargs) -> Source:
    source = Source(
        project_id=project.id,
        url=url,
        source_type=kwargs.pop("source_type", "trade_press"),
        fetched_at=dt.datetime(2026, 1, 2),
        published_at=kwargs.pop("published_at", dt.datetime(2026, 1, 1)),
        **kwargs,
    )
    session.add(source)
    session.flush()
    return source


def _rows(session, project):
    session.refresh(project)
    facts.rebuild(session, project)
    return session.query(Fact).filter(Fact.project_id == project.id).all()


def test_each_campus_statement_carries_its_tier_quote_and_whether_it_is_held(session):
    project = _campus(session)
    _source(
        session,
        project,
        "https://example.test/a",
        claims=json.dumps({"mw_planned": 1200.0, "company": "Crusoe"}),
        fields="company,mw_planned",
        quotes=json.dumps({"mw_planned": "The campus will total 1.2 GW."}),
        claim_meta=json.dumps({"mw_planned": {"scope": "this_site", "as_of": "2025-12-01"}}),
    )
    _source(
        session,
        project,
        "https://example.test/b",
        claims=json.dumps({"mw_planned": 1000.0}),
        fields="",
        unconfirmed_fields="mw_planned",
        unconfirmed_reasons=json.dumps({"mw_planned": "superseded"}),
    )
    rows = {(r.field, r.value): r for r in _rows(session, project) if r.entity == "campus"}
    quoted = rows[("mw_planned", "1200")]
    assert (quoted.tier, quoted.held, quoted.unit, quoted.number) == ("confirmed", 1, "MW", 1200.0)
    assert quoted.quote == "The campus will total 1.2 GW."
    assert quoted.as_of == dt.date(2025, 12, 1)
    assert json.loads(quoted.axes)["scope"] == "this_site"
    ruled = rows[("mw_planned", "1000")]
    assert (ruled.tier, ruled.reason, ruled.held) == ("ruled_out", "superseded", 0)


def test_tranches_parties_milestones_and_obstacles_are_statements_too(session):
    project = _campus(session)
    source = _source(
        session,
        project,
        "https://example.test/c",
        claims=json.dumps({}),
        blocks=json.dumps(
            [
                {
                    "label": "Building 1",
                    "mw": 200.0,
                    "status": "serving",
                    "quotes": {"mw": "Building 1, 200 MW, is serving."},
                }
            ]
        ),
        parties=json.dumps(
            [
                {"name": "Oracle", "role": "customer", "quote": "Oracle leases it."},
                {"name": "Lancium", "role": "owner", "unconfirmed": "no_quote"},
            ]
        ),
    )
    session.add(
        Event(
            project_id=project.id,
            event_type="energized",
            event_date=dt.date(2025, 9, 30),
            description="Energised",
            quote="Energised.",
            source_id=source.id,
        )
    )
    session.add(
        Risk(
            project_id=project.id,
            category="grid_capacity",
            severity="watch",
            summary="Grid",
            first_seen=dt.date(2025, 1, 1),
            source_id=source.id,
            quote="ERCOT flagged it.",
        )
    )
    session.flush()
    rows = _rows(session, project)
    by = {(r.entity, r.entity_key, r.field): r for r in rows}
    assert by[("tranche", "building-1", "mw")].tier == "confirmed"
    assert by[("party", "oracle", "customer")].quote == "Oracle leases it."
    assert by[("party", "lancium", "owner")].tier == "unconfirmed"
    assert by[("milestone", "energized", "event_date")].value == "2025-09-30"
    assert by[("obstacle", "grid_capacity", "severity")].value == "watch"


def test_a_directory_and_a_derived_source_are_told_apart(session):
    project = _campus(session)
    _source(
        session,
        project,
        "https://www.datacentermap.com/usa/texas/abilene/",
        claims=json.dumps({"mw_planned": 1200.0}),
        fields="mw_planned",
    )
    _source(
        session,
        project,
        "census:place/4801000",
        extractor="derived:census-place-2020",
        claims=json.dumps({"county": "Taylor County"}),
        fields="county",
    )
    tiers = {r.field: r.tier for r in _rows(session, project) if r.entity == "campus"}
    assert tiers == {"mw_planned": "directory", "county": "derived"}


def test_a_second_rebuild_changes_nothing(session):
    project = _campus(session)
    _source(
        session,
        project,
        "https://example.test/a",
        claims=json.dumps({"mw_planned": 1200.0}),
        fields="mw_planned",
    )
    first = [(r.entity, r.field, r.value, r.tier) for r in _rows(session, project)]
    second = [(r.entity, r.field, r.value, r.tier) for r in _rows(session, project)]
    assert first == second and len(first) == 1


def test_the_download_spells_out_each_statement(session):
    project = _campus(session)
    _source(
        session,
        project,
        "https://example.test/a",
        claims=json.dumps({"mw_planned": 1200.0}),
        fields="mw_planned",
    )
    _rows(session, project)
    text = facts.render_csv(facts.rows_for(session, [project.id]))
    header, line = text.splitlines()
    assert header.split(",") == list(facts.CSV_COLUMNS)
    assert ",Crusoe,Abilene,campus,,mw_planned,1200,1200.0,MW,confirmed,,True," in line


# --- the surfaces read the table, and say what the JSON said -------------------

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)


def _abilene() -> IngestRecord:
    """A campus whose citations disagree in every way the merge distinguishes."""
    first_hand = SourceRecord(
        url="https://press.example.test/abilene-1-2gw",
        source_type="company_filing",
        fetched_at=T0,
        excerpt="Crusoe's Abilene campus will total 1.2 GW for Oracle.",
        claims={
            "name": "Abilene",
            "company": "Crusoe",
            "customer": "Oracle",
            "city": "Abilene",
            "state": "TX",
            "phase": "construction",
            "mw_planned": 1200.0,
            "investment_usd": 15_000_000_000,
            "expected_online": "2026-07-01",
        },
        quotes={
            "mw_planned": "The campus will total 1.2 GW.",
            "investment_usd": "a $15 billion build",
            "expected_online": "online in the third quarter of 2026",
        },
        claim_meta={
            "mw_planned": {"scope": "this_site", "as_of": "2025-12-01", "basis": "it_load"},
            "expected_online": {"date_precision": "quarter"},
        },
        blocks=[
            BlockRecord(
                label="Building 1",
                mw=200.0,
                status="serving",
                quotes={"mw": "Building 1, 200 MW, is serving."},
            )
        ],
        parties=[
            PartyRecord(name="Oracle", role="customer", quote="Oracle leases the campus."),
            PartyRecord(name="Lancium", role="owner", quote="Lancium owns the land."),
        ],
    )
    unquoted = SourceRecord(
        url="https://news.example.test/abilene-1gw",
        source_type="trade_press",
        fetched_at=T0 + dt.timedelta(days=30),
        excerpt="A 1 GW campus in Abilene.",
        claims={"company": "Crusoe", "state": "TX", "city": "Abilene", "mw_planned": 1000},
        unconfirmed=frozenset({"mw_planned"}),
        unconfirmed_reasons=(("mw_planned", "no_quote"),),
    )
    one_building = SourceRecord(
        url="https://news.example.test/abilene-building-2",
        source_type="trade_press",
        fetched_at=T0 + dt.timedelta(days=60),
        excerpt="Building 2 adds 200 MW, more than 150 MW of it running.",
        claims={
            "company": "Crusoe",
            "state": "TX",
            "city": "Abilene",
            "mw_planned": 200.0,
            "mw_built": 150.0,
        },
        quotes={"mw_planned": "Building 2 adds 200 MW", "mw_built": "more than 150 MW running"},
        claim_meta={
            "mw_planned": {"scope": "building"},
            "mw_built": {"bound": "at_least", "modality": "operating"},
        },
    )
    directory = SourceRecord(
        url="https://www.datacentermap.com/usa/texas/abilene/",
        source_type="trade_press",
        fetched_at=T0,
        excerpt="Abilene, 300 MW built.",
        claims={"company": "Crusoe", "state": "TX", "city": "Abilene", "mw_built": 300.0},
        quotes={"mw_built": "300 MW built"},
    )
    census = SourceRecord(
        url="census:place/4801000",
        source_type="government_doc",
        fetched_at=T0,
        extractor="derived:census-place-2020",
        claims={"county": "Taylor County", "state": "TX", "city": "Abilene", "company": "Crusoe"},
    )
    return IngestRecord(
        project={"company": "Crusoe", "name": "Abilene", "city": "Abilene", "state": "TX"},
        sources=[first_hand, unquoted, one_building, directory, census],
        events=[
            EventRecord(
                event_date=dt.date(2025, 9, 30),
                event_type="energized",
                description="Building 1 energised",
                source_url=first_hand.url,
                quote="Building 1, 200 MW, is serving.",
            )
        ],
        risks=[
            RiskRecord(
                category="grid_capacity",
                severity="watch",
                summary="ERCOT flagged the load",
                quote="ERCOT flagged it.",
                first_seen=dt.date(2025, 1, 1),
                source_url=unquoted.url,
            )
        ],
    )


def _fairwater() -> IngestRecord:
    """Whole and fractional capacities, which the claim table prints as written."""
    return IngestRecord(
        project={
            "company": "Microsoft",
            "name": "Fairwater",
            "city": "Mount Pleasant",
            "state": "WI",
        },
        sources=[
            SourceRecord(
                url="https://news.example.test/fairwater",
                source_type="manual",
                fetched_at=T0,
                excerpt="Fairwater: 300 MW, each building exceeds 350 MW.",
                claims={
                    "name": "Fairwater",
                    "company": "Microsoft",
                    "city": "Mount Pleasant",
                    "state": "WI",
                    "phase": "operational",
                    "mw_planned": 300,
                    "lat": 42.71,
                    "lon": -87.93,
                    "first_announced": "2023-05-01",
                },
                quotes={"mw_planned": "Each exceeds 350 MW", "phase": "is operational"},
                claim_meta={"first_announced": {"date_precision": "month"}},
            ),
            SourceRecord(
                url="https://other.example.test/fairwater",
                source_type="general_media",
                fetched_at=T0 + dt.timedelta(days=5),
                excerpt="Fairwater is under construction at 320.5 MW.",
                claims={
                    "company": "Microsoft",
                    "city": "Mount Pleasant",
                    "state": "WI",
                    "phase": "construction",
                    "mw_planned": 320.5,
                },
                unconfirmed=frozenset({"phase"}),
                unconfirmed_reasons=(("phase", "quote_off_target"),),
                quotes={"mw_planned": "at 320.5 MW"},
            ),
        ],
    )


def _fleet(session) -> list[Project]:
    from tracker.conflicts import supersede
    from tracker.upsert import recompute_from_sources, upsert_record

    ids = [upsert_record(session, rec).project_id for rec in (_abilene(), _fairwater())]
    abilene = session.get(Project, ids[0])
    # A ruling as well as a refusal: the unquoted 1 GW figure decided against.
    loser = next(s for s in abilene.sources if s.url.endswith("abilene-1gw"))
    supersede(loser, "mw_planned")
    session.flush()
    recompute_from_sources(session, abilene)
    session.commit()
    return [session.get(Project, pid) for pid in ids]


def test_read_from_the_table_every_surface_says_what_the_json_said(session):
    """The same algorithm over both records gives the same answer, type and order.

    The JSON reader (`gaps.FromCitations`, `upsert.claims_by_field`) is what every
    surface read before; the table reader (`facts.Statements`) is what they read
    now. Compared as JSON text, so 300 and 300.0 are told apart.
    """
    from tracker import export, gaps
    from tracker.upsert import claims_by_field
    from tracker.vocab import WRITABLE_FIELDS

    for project in _fleet(session):
        from_json = gaps.FromCitations()
        json_claims = claims_by_field(list(project.sources))
        said = facts.Statements(project)
        table_claims = said.claims_by_field()
        # Each field's claims, in the merge's order, typed as the citation wrote them.
        assert {f: repr(c) for f, c in table_claims.items()} == {
            f: repr(c) for f, c in json_claims.items()
        }
        for field in WRITABLE_FIELDS:
            assert gaps.provenance(project, field, table_claims, said) == gaps.provenance(
                project, field
            ), field
            for source in project.sources:
                for ask in ("tier", "asserted", "quote", "axes", "reason"):
                    assert getattr(said, ask)(source, field) == getattr(from_json, ask)(
                        source, field
                    ), (ask, field, source.url)
        assert json.dumps(export._provenance_json(project, said, table_claims)) == json.dumps(
            export._provenance_json(project, from_json, json_claims)
        )
        assert json.dumps(export._claims_json(project, said, table_claims)) == json.dumps(
            export._claims_json(project, from_json, json_claims)
        )


def test_the_fleet_exercises_what_the_comparison_is_for(session):
    """Guards the test above against passing on a fleet too plain to differ."""
    from tracker import export

    abilene, fairwater = _fleet(session)
    claims = export.to_json_object(abilene)["claims_by_field"]["mw_planned"]["claims"]
    assert {(c["value"], c["confirmed"], c["part"], c["unconfirmed_reason"]) for c in claims} == {
        (1200.0, True, False, None),
        (1000, False, False, "superseded"),
        (200.0, True, True, None),
    }
    assert export.to_json_object(abilene)["prov"]["county"]["tier"] == "derived"
    raw = json.dumps(export.to_json_object(fairwater)["claims_by_field"]["mw_planned"])
    assert '"value": 300,' in raw and '"value": 320.5,' in raw


def test_the_api_reads_each_field_from_the_table(session):
    from tracker.webui import publicapi

    abilene, _ = _fleet(session)
    detail = publicapi.project_detail(abilene, "/v1")
    assert detail["fields"]["mw_planned"]["quote"] == "The campus will total 1.2 GW."
    # Change what the table says behind the JSON's back: the API follows the table.
    session.execute(
        Fact.__table__.update()
        .where(Fact.project_id == abilene.id, Fact.field == "mw_planned", Fact.value == "1200")
        .values(quote="What the table says.")
    )
    assert publicapi.project_detail(abilene, "/v1")["fields"]["mw_planned"]["quote"] == (
        "What the table says."
    )


# --- no write leaves the table behind -------------------------------------------


def _current(session) -> None:
    session.commit()
    assert facts.drifted(session) == []


def test_the_check_sees_a_table_the_sources_no_longer_match(session):
    """Guards the sequence below against a check that could never fail."""
    abilene, _ = _fleet(session)
    session.execute(
        Fact.__table__.update()
        .where(Fact.project_id == abilene.id, Fact.field == "mw_planned")
        .values(tier="directory")
    )
    assert facts.drifted(session) == [abilene.id]


def test_every_kind_of_write_leaves_the_statements_current(session):
    """A representative sequence of the writes that change what a campus's sources
    say, or what it holds, each followed by the check that the stored statements are
    what a rebuild would write now.

    Several of these paths never rebuilt the table themselves — a party's role ended
    and resumed, an alias added and taken back, a publication date learned, a
    backfill rewriting an envelope or a gate's verdict, an obstacle resolved by
    hand — and the table now follows every campus a transaction touched
    (`facts.follow`).
    """
    from tracker import audit, companies, dates, exclude, parties
    from tracker import blocks as blocks_mod
    from tracker.merge import merge_projects
    from tracker.upsert import recompute_from_sources, upsert_record

    abilene, fairwater = _fleet(session)
    _current(session)

    # An ingest that adds a citation.
    upsert_record(
        session,
        IngestRecord(
            project={"company": "Crusoe", "name": "Abilene", "city": "Abilene", "state": "TX"},
            sources=[
                SourceRecord(
                    url="https://news.example.test/abilene-update",
                    source_type="trade_press",
                    fetched_at=T0 + dt.timedelta(days=90),
                    claims={
                        "company": "Crusoe",
                        "state": "TX",
                        "city": "Abilene",
                        "mw_built": 400.0,
                    },
                    quotes={"mw_built": "400 MW now running"},
                )
            ],
        ),
    )
    _current(session)

    # A second row for the same campus, filed under the next town, folded into the first.
    dupe = upsert_record(
        session,
        IngestRecord(
            project={"company": "Crusoe", "name": "Stargate", "city": "Tye", "state": "TX"},
            sources=[
                SourceRecord(
                    url="https://other.example.test/stargate",
                    source_type="general_media",
                    fetched_at=T0,
                    claims={
                        "company": "Crusoe",
                        "name": "Stargate",
                        "state": "TX",
                        "city": "Tye",
                        "mw_planned": 1200.0,
                    },
                    quotes={"mw_planned": "Stargate's 1.2 GW"},
                )
            ],
        ),
        force_new=True,
    ).project_id
    assert dupe != abilene.id
    _current(session)
    merge_projects(session, abilene.id, [dupe])
    _current(session)

    # The fold taken back for the next reading (`tracker unalias`): routing only.
    from tracker import logic as logic_mod
    from tracker.models import ProjectAlias

    alias = session.query(ProjectAlias).filter(ProjectAlias.to_project_id == abilene.id).first()
    session.delete(alias)
    logic_mod.record_decision(
        session.get(Project, abilene.id), "operator_unaliased", "key no longer routes here"
    )
    _current(session)

    # A ruling against a claim (`logic rule-out`, the audit's repairs).
    abilene = session.get(Project, abilene.id)
    directory = next(s for s in abilene.sources if "datacentermap" in s.url)
    audit._rule_against(session, abilene, "mw_built", [directory], "a directory's figure")
    _current(session)

    # A person's ruling on how a tranche nests (`tracker tranches rule`).
    from tracker import nesting

    nesting.rule(session, abilene, "building-1", "separate", by="nathan", reason="its own hall")
    _current(session)

    # A ruling against a citation's tranches (`logic rule-out ID blocks`).
    first = next(s for s in abilene.sources if s.url.endswith("abilene-1-2gw"))
    blocks_mod.rule_out(first, reason="misread")
    session.flush()
    blocks_mod.rebuild(session, abilene)
    recompute_from_sources(session, abilene)
    _current(session)

    # A party's role ended, and taken back.
    parties.end_role(session, abilene, "Oracle", "customer", reason="lease ended", by="nathan")
    _current(session)
    parties.resume_role(session, abilene, "Oracle", "customer")
    _current(session)

    # A spelling folded into another company, and the fold taken back (`tracker unalias`).
    companies.sync(session)
    companies.add_alias(session, "Lancium", companies.find(session, "Crusoe"), by="nathan")
    _current(session)
    companies.remove_alias(session, "Lancium")
    _current(session)

    # A publication date learned for a citation (`backfill dates`).
    dates._propagate(session, {first.url: dt.datetime(2025, 11, 1)}, apply=True)
    _current(session)

    # A backfill rewriting a claim's envelope, and one changing a gate's verdict,
    # neither re-deriving the row (`backfill scope` leaves that to `backfill derive`).
    fair = session.get(Project, fairwater.id)
    source = next(s for s in fair.sources if s.url.endswith("news.example.test/fairwater"))
    source.claim_meta = json.dumps({"mw_planned": {"bound": "at_least", "scope": "this_site"}})
    _current(session)
    other = next(s for s in fair.sources if s.url.endswith("other.example.test/fairwater"))
    other.unconfirmed_fields = None
    other.fields = "company,city,state,phase,mw_planned"
    _current(session)

    # An obstacle resolved and a milestone corrected by hand.
    risk = session.query(Risk).filter(Risk.project_id == abilene.id).one()
    risk.status = "resolved"
    _current(session)
    event = session.query(Event).filter(Event.project_id == abilene.id).one()
    event.unconfirmed = "no_quote"
    _current(session)

    # A campus excluded takes its statements with it.
    exclude.exclude(session, [fairwater.id], reason="not a data center")
    _current(session)
    assert session.query(Fact).filter(Fact.project_id == fairwater.id).count() == 0


def test_a_reader_in_the_writing_transaction_sees_the_write(session):
    """Before the commit, too: the export after an edit, in the same transaction."""
    from tracker import export

    abilene, _ = _fleet(session)
    source = next(s for s in abilene.sources if s.url.endswith("abilene-1-2gw"))
    quotes = json.loads(source.quotes)
    quotes["mw_planned"] = "The campus will total 1.2 gigawatts."
    source.quotes = json.dumps(quotes)
    session.flush()
    prov = export.to_json_object(abilene, claims=False)["prov"]["mw_planned"]
    assert prov["quote"] == "The campus will total 1.2 gigawatts."


def test_a_rolled_back_write_leaves_nothing_to_rebuild(session):
    abilene, _ = _fleet(session)
    abilene.sources[0].claims = json.dumps({"mw_planned": 1.0})
    session.flush()
    session.rollback()
    session.commit()
    assert facts.drifted(session) == []


@pytest.mark.parametrize("entity", ["tranche", "party", "milestone", "obstacle"])
def test_only_a_campus_statement_carries_the_claims_own_record(session, entity):
    _fleet(session)
    rows = session.query(Fact).filter(Fact.entity == entity).all()
    assert rows and all((r.value_json, r.quoted, r.doubted) == (None, None, None) for r in rows)
