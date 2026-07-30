"""The shared write path: dedup, merge policy, Q2 conflicts, idempotence.

`test_reingest_is_idempotent` is the load-bearing test of the whole design. If
it passes, the dedup key, the (project_id, url) source key, the recompute-from-
claims merge and the managed notes block are all order- and repeat-stable. If it
fails, one of them is not.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tracker.ingest.records import EventRecord, IngestRecord, RiskRecord, SourceRecord
from tracker.models import Event, Project, Risk, Source
from tracker.upsert import (
    NOTE_PREFIX,
    SOURCE_NOTE_PREFIX,
    derive_fields,
    recompute_confidence,
    upsert_record,
)
from tracker.vocab import TRACKED_FIELDS

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)
T1 = dt.datetime(2026, 6, 1, 12, 0, 0)


def rec(
    *,
    company="Microsoft",
    name="Fairwater",
    city="Mount Pleasant",
    county=None,
    state="WI",
    sources=None,
    events=None,
    risks=None,
    notes=None,
    confidence_cap=None,
    **project_extra,
):
    project = {
        "company": company,
        "name": name,
        "city": city,
        "county": county,
        "state": state,
        "country": "US",
        **project_extra,
    }
    return IngestRecord(
        project=project,
        sources=sources if sources is not None else [manual_source()],
        events=events or [],
        risks=risks or [],
        notes=notes or [],
        confidence_cap=confidence_cap,
    )


def manual_source(url="https://news.microsoft.com/fairwater/", **claims):
    base = {
        "name": "Fairwater",
        "company": "Microsoft",
        "city": "Mount Pleasant",
        "state": "WI",
        "phase": "construction",
    }
    base.update(claims)
    return SourceRecord(
        url=url, source_type="manual", fetched_at=T0, excerpt="A quote.", claims=base
    )


def counts(session):
    return (
        session.scalar(select(func.count()).select_from(Project)),
        session.scalar(select(func.count()).select_from(Source)),
    )


# --- derive_fields ----------------------------------------------------------


def test_derive_fields_is_canonically_ordered():
    """Stable ordering is what makes re-ingest byte-identical."""
    a = derive_fields({"phase": "x", "name": "y", "mw_planned": 1})
    b = derive_fields({"mw_planned": 1, "name": "y", "phase": "x"})
    assert a == b == "name,mw_planned,phase"


def test_derive_fields_skips_nulls_and_unknown_keys():
    assert derive_fields({"name": "y", "mw_planned": None, "bogus": 1}) == "name"
    assert derive_fields({}) is None


# --- Insert and idempotence -------------------------------------------------


def test_insert_creates_project_and_source(session):
    result = upsert_record(session, rec())
    assert result.action == "insert"
    assert counts(session) == (1, 1)
    project = session.get(Project, result.project_id)
    assert project.company == "Microsoft"
    assert project.city == "Mount Pleasant"
    assert project.state == "WI"
    assert project.phase == "construction"
    assert project.dedup_key == "microsoft|city:mount pleasant|WI"


def test_reingest_is_idempotent(session):
    """Re-running the same input must change nothing at all, including updated_at.

    This single assertion protects the dedup key, the source uniqueness key, the
    recompute-from-claims merge, and the managed notes block simultaneously.
    """
    first = upsert_record(session, rec())
    session.commit()
    project = session.get(Project, first.project_id)
    before = (
        {f: getattr(project, f) for f in TRACKED_FIELDS},
        project.updated_at,
        project.confidence,
        project.notes,
        sorted((s.url, s.claims, s.fields) for s in project.sources),
    )

    second = upsert_record(session, rec())
    session.commit()
    session.refresh(project)
    after = (
        {f: getattr(project, f) for f in TRACKED_FIELDS},
        project.updated_at,
        project.confidence,
        project.notes,
        sorted((s.url, s.claims, s.fields) for s in project.sources),
    )

    assert second.action == "unchanged"
    assert second.project_id == first.project_id
    assert after == before
    assert counts(session) == (1, 1)


def test_differently_spelled_company_updates_rather_than_duplicates(session):
    upsert_record(session, rec(company="Microsoft"))
    result = upsert_record(session, rec(company="Microsoft Corporation"))
    assert result.action in {"unchanged", "update"}
    assert counts(session)[0] == 1


def test_new_url_adds_a_source_without_adding_a_project(session):
    upsert_record(session, rec())
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://www.datacenterdynamics.com/a",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"mw_planned": 900.0},
                )
            ]
        ),
    )
    assert counts(session) == (1, 2)


# --- Merge policy -----------------------------------------------------------


def test_fill_only_field_is_not_churned_by_a_later_source(session):
    """Identity fields prefer stability: the first name we recorded stays."""
    upsert_record(session, rec(name="Fairwater"))
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://www.dcd.com/a",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"name": "Microsoft Racine County Campus"},
                )
            ]
        ),
    )
    project = session.scalar(select(Project))
    assert project.name == "Fairwater"


def test_prefer_weight_gives_the_field_to_the_stronger_source(session):
    """PRD Q2: the project field takes the higher-confidence value."""
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://www.pjm.com/q#AG1",
                    source_type="iso_queue",
                    fetched_at=T1,
                    claims={"mw_planned": 300.0},
                ),
                SourceRecord(
                    url="https://news.microsoft.com/x",
                    source_type="company_filing",
                    fetched_at=T0,
                    claims={"mw_planned": 900.0},
                ),
            ]
        ),
    )
    project = session.scalar(select(Project))
    assert project.mw_planned == 900.0, "company_filing (3) must beat iso_queue (1)"


def test_mw_built_takes_the_maximum(session):
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://a.com/1",
                    source_type="trade_press",
                    fetched_at=T0,
                    claims={"mw_built": 100.0},
                ),
                SourceRecord(
                    url="https://b.com/2",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"mw_built": 250.0},
                ),
            ]
        ),
    )
    assert session.scalar(select(Project)).mw_built == 250.0


def test_first_announced_takes_the_earliest(session):
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://a.com/1",
                    source_type="trade_press",
                    fetched_at=T0,
                    claims={"first_announced": dt.date(2024, 6, 1)},
                ),
                SourceRecord(
                    url="https://b.com/2",
                    source_type="company_filing",
                    fetched_at=T1,
                    claims={"first_announced": dt.date(2023, 3, 1)},
                ),
            ]
        ),
    )
    project = session.scalar(select(Project))
    assert project.first_announced == dt.date(2023, 3, 1)
    assert isinstance(project.first_announced, dt.date), (
        "must survive the JSON round-trip as a date"
    )


def test_phase_takes_the_furthest_along(session):
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://a.com/1",
                    source_type="trade_press",
                    fetched_at=T0,
                    claims={"phase": "permitting"},
                ),
                SourceRecord(
                    url="https://b.com/2",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"phase": "construction"},
                ),
            ]
        ),
    )
    assert session.scalar(select(Project)).phase == "construction"


def test_cancelled_overrides_a_further_along_phase(session):
    """A cancelled project is cancelled, even though "operational" ranks higher."""
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://a.com/1",
                    source_type="trade_press",
                    fetched_at=T0,
                    claims={"phase": "operational"},
                ),
                SourceRecord(
                    url="https://b.com/2",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"phase": "cancelled"},
                ),
            ]
        ),
    )
    assert session.scalar(select(Project)).phase == "cancelled"


def test_ingest_order_does_not_change_the_result(session, engine, db_path):
    """Recompute-from-claims means PJM-then-news equals news-then-PJM."""
    iso = SourceRecord(
        url="https://www.pjm.com/q#AG1",
        source_type="iso_queue",
        fetched_at=T0,
        claims={"mw_planned": 300.0, "phase": "permitting"},
    )
    press = SourceRecord(
        url="https://www.dcd.com/a",
        source_type="trade_press",
        fetched_at=T1,
        claims={"mw_planned": 450.0, "phase": "construction"},
    )

    upsert_record(session, rec(sources=[iso]))
    upsert_record(session, rec(sources=[press]))
    forward = session.scalar(select(Project))
    forward_state = (forward.mw_planned, forward.phase, forward.confidence)

    # Fresh database, reversed order.
    from tracker.db import init_db, session_scope

    other_engine, _ = init_db(db_path.parent / "reversed.db")
    with session_scope(other_engine) as s2:
        upsert_record(s2, rec(sources=[press]))
        upsert_record(s2, rec(sources=[iso]))
        reverse = s2.scalar(select(Project))
        reverse_state = (reverse.mw_planned, reverse.phase, reverse.confidence)

    assert forward_state == reverse_state


# --- Q2 conflict handling ---------------------------------------------------


def test_conflict_keeps_both_sources_and_discloses_the_spread(session):
    """PRD open question Q2, end to end."""
    result = upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://www.pjm.com/q#AG1",
                    source_type="iso_queue",
                    fetched_at=T0,
                    claims={"mw_planned": 300.0},
                ),
                SourceRecord(
                    url="https://www.dcd.com/a",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"mw_planned": 450.0},
                ),
            ]
        ),
    )
    project = session.get(Project, result.project_id)

    assert "mw_planned" in result.conflicts
    assert len(project.sources) == 2, "both claims must survive in their own source rows"
    assert {json.loads(s.claims)["mw_planned"] for s in project.sources} == {300.0, 450.0}
    assert project.mw_planned == 450.0, "trade_press (2) outweighs iso_queue (1)"
    assert f"{NOTE_PREFIX} conflict mw_planned" in project.notes
    assert "33% spread" in project.notes


def test_values_within_tolerance_are_not_flagged(session):
    result = upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://a.com/1",
                    source_type="trade_press",
                    fetched_at=T0,
                    claims={"mw_planned": 900.0},
                ),
                SourceRecord(
                    url="https://b.com/2",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"mw_planned": 1000.0},
                ),
            ]
        ),
    )
    assert result.conflicts == []


def test_resolved_conflict_disclosure_disappears(session):
    """Managed note lines are rebuilt, not appended, so stale ones do not linger."""
    conflicting = [
        SourceRecord(
            url="https://www.pjm.com/q#AG1",
            source_type="iso_queue",
            fetched_at=T0,
            claims={"mw_planned": 300.0},
        ),
        SourceRecord(
            url="https://www.dcd.com/a",
            source_type="trade_press",
            fetched_at=T1,
            claims={"mw_planned": 450.0},
        ),
    ]
    upsert_record(session, rec(sources=conflicting))
    project = session.scalar(select(Project))
    assert "conflict mw_planned" in project.notes

    # The trade press corrects itself to match the queue.
    upsert_record(
        session,
        rec(
            sources=[
                conflicting[0],
                SourceRecord(
                    url="https://www.dcd.com/a",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"mw_planned": 300.0},
                ),
            ]
        ),
    )
    session.refresh(project)
    assert "conflict mw_planned" not in (project.notes or "")


def test_operator_prose_in_notes_is_preserved(session):
    upsert_record(session, rec())
    project = session.scalar(select(Project))
    project.notes = "Spoke to the county planner; expansion is real.\n" + (project.notes or "")
    session.flush()

    upsert_record(session, rec(notes=["queue MW is generator nameplate"]))
    session.refresh(project)
    assert "Spoke to the county planner" in project.notes
    assert SOURCE_NOTE_PREFIX in project.notes
    assert "queue MW is generator nameplate" in project.notes


def test_contributed_notes_accumulate_across_records(session):
    """Two records for one project must not erase each other's disclosures.

    Without this, `updated_at` churns on every single ingest run: each record
    rewrites the notes block, the next run flips it back, and nothing ever
    settles. Derived lines (conflicts) are rebuilt; contributed lines accumulate.
    """
    upsert_record(session, rec(notes=["first disclosure"]))
    upsert_record(
        session,
        rec(sources=[manual_source("https://other.example/x")], notes=["second disclosure"]),
    )
    project = session.scalar(select(Project))
    assert "first disclosure" in project.notes
    assert "second disclosure" in project.notes

    # And the whole thing must now be stable.
    before = project.updated_at
    upsert_record(session, rec(notes=["first disclosure"]))
    session.refresh(project)
    assert project.updated_at == before


# --- The city/county duplicate proposal -------------------------------------


def test_county_row_does_not_auto_merge_into_a_city_row(session):
    """The PRD's High risk, encoded as behaviour rather than a README caveat."""
    upsert_record(session, rec(city="Racine", county=None))
    result = upsert_record(
        session,
        rec(
            city=None,
            county="Racine County",
            sources=[
                SourceRecord(
                    url="https://www.pjm.com/q#AG1",
                    source_type="iso_queue",
                    fetched_at=T0,
                    claims={"phase": "permitting"},
                )
            ],
        ),
    )

    assert counts(session)[0] == 2, "must not merge across a county/city boundary"
    assert result.duplicate_of is not None
    project = session.get(Project, result.project_id)
    assert "possible duplicate of project #" in project.notes
    assert project.confidence <= 1, "an unresolved identity question caps confidence"


def test_duplicate_proposal_survives_reingest(session):
    upsert_record(session, rec(city="Racine"))
    iso = [
        SourceRecord(
            url="https://www.pjm.com/q#AG1",
            source_type="iso_queue",
            fetched_at=T0,
            claims={"phase": "permitting"},
        )
    ]
    upsert_record(session, rec(city=None, county="Racine County", sources=iso))
    upsert_record(session, rec(city=None, county="Racine County", sources=iso))

    county_row = session.scalar(select(Project).where(Project.county.is_not(None)))
    assert "possible duplicate" in county_row.notes
    assert county_row.notes.count("possible duplicate") == 1, "must not accumulate"


def test_a_city_row_that_knows_its_county_is_matched_against_county_rows(session):
    """The PRD's flagship duplicate case, which name comparison alone misses.

    A news record says "Mount Pleasant, WI" and also knows it is in Racine County.
    A queue record only ever says "Racine". The locality *names* share nothing, so
    only the shared county-granular alternate key connects them.
    """
    upsert_record(session, rec(city="Mount Pleasant", county="Racine County"))
    result = upsert_record(
        session,
        rec(
            city=None,
            county="Racine",
            sources=[
                SourceRecord(
                    url="https://www.pjm.com/q#AG1",
                    source_type="iso_queue",
                    fetched_at=T0,
                    claims={"phase": "permitting"},
                )
            ],
        ),
    )
    assert counts(session)[0] == 2, "still must not merge automatically"
    assert result.duplicate_of is not None, "but the ambiguity must be surfaced"
    assert "possible duplicate" in session.get(Project, result.project_id).notes


def test_unrelated_cities_for_one_company_are_not_flagged(session):
    """Two genuinely separate campuses must not nag the operator forever."""
    upsert_record(session, rec(company="Google", city="Council Bluffs", state="IA"))
    result = upsert_record(
        session,
        rec(
            company="Google",
            city="Cedar Rapids",
            state="IA",
            sources=[manual_source("https://blog.google/cedar")],
        ),
    )
    assert result.duplicate_of is None
    assert counts(session)[0] == 2


def test_force_new_bypasses_duplicate_detection(session):
    upsert_record(session, rec(city="Racine"))
    result = upsert_record(
        session,
        rec(
            city=None,
            county="Racine County",
            sources=[
                SourceRecord(
                    url="https://www.pjm.com/q#AG1",
                    source_type="iso_queue",
                    fetched_at=T0,
                    claims={"phase": "permitting"},
                )
            ],
        ),
        force_new=True,
    )
    assert result.duplicate_of is None
    assert counts(session)[0] == 2


# --- Events -----------------------------------------------------------------


def test_events_are_written_and_linked_to_their_source(session):
    url = "https://news.microsoft.com/fairwater/"
    result = upsert_record(
        session,
        rec(
            sources=[manual_source(url)],
            events=[EventRecord(dt.date(2023, 3, 1), "announced", "Announced.", url)],
        ),
    )
    assert result.events_written == 1
    event = session.scalar(select(Event))
    assert event.project_id == result.project_id
    assert event.source_id is not None
    assert session.get(Source, event.source_id).url == url


def test_reingesting_events_does_not_duplicate_them(session):
    url = "https://news.microsoft.com/fairwater/"
    payload = rec(
        sources=[manual_source(url)],
        events=[EventRecord(dt.date(2023, 3, 1), "announced", "Announced.", url)],
    )
    upsert_record(session, payload)
    second = upsert_record(session, payload)
    assert second.events_written == 0
    assert session.scalar(select(func.count()).select_from(Event)) == 1


def test_event_with_unknown_source_url_still_records(session):
    """A milestone without a resolvable citation is still a fact worth keeping."""
    result = upsert_record(
        session,
        rec(
            events=[
                EventRecord(
                    dt.date(2023, 3, 1), "announced", "Announced.", "https://elsewhere.example/x"
                )
            ]
        ),
    )
    event = session.scalar(select(Event))
    assert event.source_id is None
    assert result.events_written == 1


# --- Confidence -------------------------------------------------------------


def test_manual_seed_lands_at_confidence_two(session):
    """PRD open question Q1."""
    upsert_record(
        session,
        rec(
            mw_planned=900.0,
            sources=[
                manual_source(
                    mw_planned=900.0,
                    investment_usd=3_300_000_000,
                    first_announced="2023-03-01",
                    expected_online="2026-01-01",
                ),
            ],
        ),
    )
    assert session.scalar(select(Project)).confidence == 2


def test_confidence_cap_is_respected(session):
    """The ISO path knows its own facts are weak and says so."""
    upsert_record(
        session,
        rec(
            confidence_cap=1,
            sources=[
                manual_source(mw_planned=900.0, investment_usd=1, first_announced="2023-03-01"),
            ],
        ),
    )
    assert session.scalar(select(Project)).confidence == 1


def test_confidence_cache_is_consistent(session):
    """Stored confidence must equal recomputed confidence, or the cache is lying."""
    upsert_record(session, rec())
    upsert_record(
        session,
        rec(
            company="Google",
            city="Council Bluffs",
            state="IA",
            sources=[manual_source("https://blog.google/x")],
        ),
    )
    session.flush()
    assert recompute_confidence(session) == 0


# --- Traceability -----------------------------------------------------------


def test_every_field_is_cited(session):
    """The executable form of the PRD's central premise: no uncited facts.

    Every non-NULL tracked field on a project must be traceable to a citation.
    For eleven of the twelve that means appearing in some source's `fields`.
    `blocker` is the exception: it is derived from the `risk` rows rather than
    merged from claims, so its citation lives in `risk.source_id`. The invariant is
    unchanged — only where the evidence is recorded moved.
    """
    upsert_record(
        session,
        rec(
            sources=[
                manual_source(
                    mw_planned=900.0,
                    investment_usd=3_300_000_000,
                    first_announced="2023-03-01",
                ),
                SourceRecord(
                    url="https://www.dcd.com/a",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"mw_built": 150.0, "customer": "Microsoft"},
                ),
            ],
            risks=[
                RiskRecord(
                    category="transmission",
                    severity="material",
                    summary="Transmission upgrades pending.",
                    quote="two 345-kilovolt upgrades",
                    source_url="https://news.microsoft.com/fairwater/",
                )
            ],
        ),
    )
    for project in session.scalars(select(Project)).all():
        cited: set[str] = set()
        for s in project.sources:
            if s.fields:
                cited.update(p.strip() for p in s.fields.split(","))
        source_ids = {s.id for s in project.sources}
        if any(r.source_id in source_ids for r in project.risks):
            cited.add("blocker")
        populated = {f for f in TRACKED_FIELDS if getattr(project, f) is not None}
        assert populated <= cited, f"uncited fields on project {project.id}: {populated - cited}"
        assert project.blocker == "Transmission upgrades pending."


# --- Risks and the derived blocker ------------------------------------------


def risk(category="transmission", severity="material", summary="Upgrades pending.", **kw):
    kw.setdefault("source_url", "https://news.microsoft.com/fairwater/")
    return RiskRecord(category=category, severity=severity, summary=summary, **kw)


def test_a_risk_is_written_and_cited(session):
    result = upsert_record(session, rec(risks=[risk(quote="two 345-kilovolt upgrades")]))
    assert result.risks_written == 1
    row = session.scalar(select(Risk))
    assert (row.category, row.severity, row.status) == ("transmission", "material", "open")
    assert row.quote == "two 345-kilovolt upgrades"
    assert row.source_id == session.scalar(select(Source.id))


def test_the_blocker_is_the_most_severe_open_risk(session):
    upsert_record(
        session,
        rec(
            risks=[
                risk(category="water", severity="watch", summary="Draw questioned."),
                risk(category="permitting", severity="blocking", summary="Rezoning refused."),
                risk(category="financing", severity="material", summary="Round unclosed."),
            ]
        ),
    )
    assert session.scalar(select(Project)).blocker == "Rezoning refused."


def test_resolving_a_risk_clears_the_blocker(session):
    """What the free-text column could never do.

    `_resolve` returns the existing value when a field has no claims, so the old
    `blocker` string could be replaced but never set back to NULL — a resolved
    obstacle sat on the row forever.
    """
    upsert_record(session, rec(risks=[risk()]))
    project = session.scalar(select(Project))
    assert project.blocker == "Upgrades pending."

    session.scalar(select(Risk)).status = "resolved"
    session.flush()

    upsert_record(session, rec(risks=[]))
    assert session.scalar(select(Project)).blocker is None


def test_reingesting_the_same_risk_writes_nothing(session):
    """Idempotence, including for an undated risk.

    `first_seen` is nullable and SQLite treats NULLs as distinct, so the UNIQUE
    constraint does not dedup this case — `_upsert_risks` does, in Python.
    """
    upsert_record(session, rec(risks=[risk()]))
    second = upsert_record(session, rec(risks=[risk()]))
    assert second.risks_written == 0
    assert second.action == "unchanged"
    assert session.scalar(select(func.count()).select_from(Risk)) == 1


def test_the_same_category_on_a_new_date_is_a_second_risk(session):
    upsert_record(session, rec(risks=[risk(first_seen=dt.date(2026, 1, 1))]))
    upsert_record(session, rec(risks=[risk(first_seen=dt.date(2026, 6, 1))]))
    assert session.scalar(select(func.count()).select_from(Risk)) == 2


def test_re_reading_an_edited_article_updates_the_risk_in_place(session):
    upsert_record(session, rec(risks=[risk(severity="watch", summary="Upgrades queued.")]))
    result = upsert_record(session, rec(risks=[risk(severity="blocking", summary="Work halted.")]))
    assert result.risks_written == 0
    row = session.scalar(select(Risk))
    assert (row.severity, row.summary) == ("blocking", "Work halted.")
    assert session.scalar(select(Project)).blocker == "Work halted."


def test_an_operator_resolution_is_not_revived_by_a_re_read(session):
    """`status` belongs to the operator, not to the extractor. An article that
    still mentions a settled obstacle is not evidence it came back."""
    upsert_record(session, rec(risks=[risk()]))
    session.scalar(select(Risk)).status = "resolved"
    session.flush()

    upsert_record(session, rec(risks=[risk(summary="Upgrades still pending.")]))
    assert session.scalar(select(Risk)).status == "resolved"
    assert session.scalar(select(Project)).blocker is None


def test_an_ingest_with_no_risks_does_not_clear_existing_ones(session):
    """An article that stops mentioning an obstacle is not evidence it is gone."""
    upsert_record(session, rec(risks=[risk()]))
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://www.dcd.com/b",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"mw_planned": 900.0},
                )
            ]
        ),
    )
    assert session.scalar(select(func.count()).select_from(Risk)) == 1
    assert session.scalar(select(Project)).blocker == "Upgrades pending."


def test_risks_do_not_move_with_the_claims_merge(session):
    """`blocker` is in DERIVED_FIELDS, so a claim never sets the column.

    A source may still record a `blocker` claim — that is what keeps
    `source.fields` honest about which citation supports it — but the value is
    written and never read.
    """
    upsert_record(
        session,
        rec(sources=[manual_source(blocker="A claim nobody should read.")], risks=[]),
    )
    assert session.scalar(select(Project)).blocker is None


# --- Slippage ---------------------------------------------------------------


def _with_date(value, url="https://news.microsoft.com/fairwater/", **kw):
    return rec(sources=[manual_source(url=url, expected_online=value, **kw)])


def test_a_slip_into_a_later_year_is_recorded_as_an_event(session):
    upsert_record(session, _with_date("2027-07-01"))
    upsert_record(session, _with_date("2028-07-01", url="https://www.dcd.com/a"))

    event = session.scalar(select(Event).where(Event.event_type == "delayed"))
    assert event is not None
    assert "2027-07-01 to 2028-07-01" in event.description
    assert "+366 days" in event.description


def test_a_slip_sets_delay_days_on_the_worst_open_risk(session):
    upsert_record(session, _with_date("2027-07-01", risks=None))
    upsert_record(
        session,
        rec(
            sources=[manual_source(expected_online="2027-07-01")],
            risks=[
                risk(category="water", severity="watch", summary="Minor."),
                risk(category="transmission", severity="blocking", summary="Work stopped."),
            ],
        ),
    )
    upsert_record(
        session,
        rec(sources=[manual_source(url="https://www.dcd.com/a", expected_online="2029-01-01")]),
    )

    rows = {r.category: r.delay_days for r in session.scalars(select(Risk)).all()}
    assert rows["transmission"] is not None, "the slip belongs on the blocking risk"
    assert rows["water"] is None


def test_a_move_within_one_year_is_logged_but_not_counted(session):
    """`norm_date_detail` coarsens hedged dates, so a bare "2027" lands on
    2027-01-01 and "late 2027" on 2027-10-01. A source restating the same year more
    precisely is indistinguishable from a 273-day delay, and the column stores no
    precision to tell them apart.
    """
    # The normalized forms of a bare "2027" and of "late 2027" respectively —
    # claims reach the write path already coerced.
    upsert_record(session, rec(sources=[manual_source(expected_online="2027-01-01")]))
    upsert_record(
        session,
        rec(
            sources=[manual_source(url="https://www.dcd.com/a", expected_online="2027-10-01")],
            risks=[risk(severity="blocking")],
        ),
    )

    event = session.scalar(select(Event).where(Event.event_type == "delayed"))
    assert event is not None, "the tracked value did move, and that is worth logging"
    assert "may be a more precise restatement" in event.description
    assert session.scalar(select(Risk)).delay_days is None, "no number on an ambiguous move"


def test_a_date_moving_earlier_is_not_a_delay(session):
    upsert_record(session, _with_date("2029-01-01"))
    upsert_record(session, _with_date("2027-01-01", url="https://www.dcd.com/a"))
    assert session.scalar(select(func.count()).select_from(Event)) == 0


def test_a_first_date_is_not_a_delay(session):
    """NULL to a value is learning the timeline, not the timeline slipping."""
    upsert_record(session, _with_date("2027-07-01"))
    assert session.scalar(select(func.count()).select_from(Event)) == 0


def test_no_risk_is_invented_from_a_date_change(session):
    """A date moving says the timeline changed, not why. Manufacturing an obstacle
    would put an uncited guess into the field an operator acts on."""
    upsert_record(session, _with_date("2027-07-01"))
    upsert_record(session, _with_date("2029-01-01", url="https://www.dcd.com/a"))
    assert session.scalar(select(func.count()).select_from(Risk)) == 0
    assert session.scalar(select(Project)).blocker is None


def test_recording_a_slip_stays_idempotent(session):
    upsert_record(session, _with_date("2027-07-01"))
    upsert_record(session, _with_date("2029-01-01", url="https://www.dcd.com/a"))
    again = upsert_record(session, _with_date("2029-01-01", url="https://www.dcd.com/a"))
    assert again.action == "unchanged"
    assert session.scalar(select(func.count()).select_from(Event)) == 1


def test_expected_online_still_merges_by_source_weight(session):
    """The merge policy is untouched: the strongest source wins the value, and the
    slip is recorded as history beside it rather than by letting recency win."""
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://www.dcd.com/a",
                    source_type="general_media",
                    fetched_at=T1,
                    claims={"expected_online": "2030-01-01"},
                ),
                manual_source(expected_online="2027-07-01"),
            ]
        ),
    )
    # manual (weight 2) beats general_media (weight 1) despite being older.
    assert session.scalar(select(Project)).expected_online == dt.date(2027, 7, 1)


def test_source_fields_is_derived_not_supplied(session):
    """`fields` always matches `claims`, because it is computed from it."""
    upsert_record(session, rec(sources=[manual_source(mw_planned=900.0)]))
    source = session.scalar(select(Source))
    assert set(source.fields.split(",")) == set(json.loads(source.claims))


def test_malformed_claims_json_does_not_break_the_project(session, caplog):
    upsert_record(session, rec())
    source = session.scalar(select(Source))
    source.claims = "{not json"
    session.flush()
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://b.com/2",
                    source_type="trade_press",
                    fetched_at=T1,
                    claims={"mw_planned": 900.0},
                )
            ]
        ),
    )
    assert session.scalar(select(Project)).mw_planned == 900.0
    assert "unparseable claims" in caplog.text


# --- Schema invariants under load -------------------------------------------


def test_a_project_needs_a_locality(session):
    """ck_project_locality: a row with neither city nor county has no location."""
    with pytest.raises(IntegrityError):
        upsert_record(session, rec(city=None, county=None))
    # The failed statement poisons the transaction; roll back so the fixture's
    # commit on teardown does not raise a second, confusing error.
    session.rollback()
