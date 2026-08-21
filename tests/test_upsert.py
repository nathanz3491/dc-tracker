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
    _INGEST_ONLY_NOTES,
    NOTE_PREFIX,
    SOURCE_NOTE_PREFIX,
    derive_fields,
    recompute_confidence,
    recompute_from_sources,
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


# --- placeholders -----------------------------------------------------------
#
# Fairwater (#1), pinned. `sample-projects.json` types its unreplaced seed URLs
# `company_filing` — weight 3, the heaviest in the system, on a URL that does not
# exist. `confidence.compute` had already learned to drop them before scoring; the
# write path had not, so a placeholder set every identity field on a real project
# and was then written into `notes` as the winning side of a conflict.


PLACEHOLDER_URL = "https://news.microsoft.com/PLACEHOLDER-replace-with-the-release-you-verified/"


def placeholder_source(**claims):
    """A seed row exactly as `--allow-placeholders` admits one: heaviest type, no URL."""
    return SourceRecord(
        url=PLACEHOLDER_URL,
        source_type="company_filing",
        fetched_at=T1,
        claims=claims,
    )


def test_a_placeholder_loses_to_a_real_source_it_outweighs_on_paper(session):
    """company_filing (3) vs trade_press (2), and the lighter real source wins."""
    upsert_record(
        session,
        rec(
            sources=[
                placeholder_source(mw_planned=900.0),
                SourceRecord(
                    url="https://www.dcd.com/a",
                    source_type="trade_press",
                    fetched_at=T0,
                    claims={"mw_planned": 300.0},
                ),
            ]
        ),
    )
    assert session.scalar(select(Project)).mw_planned == 300.0


def test_a_placeholder_loses_on_the_phase_ladder_too(session):
    """The case demotion-by-weight alone would miss.

    LADDER takes the furthest-along value and never consults weight, so zeroing the
    placeholder's weight would not have saved this. It is the 待确认 flag that does
    it: `resolve` discards unconfirmed claims outright once any confirmed claim
    exists, which is true of every policy at once.
    """
    upsert_record(
        session,
        rec(
            sources=[
                placeholder_source(phase="operational"),
                SourceRecord(
                    url="https://www.dcd.com/a",
                    source_type="trade_press",
                    fetched_at=T0,
                    claims={"phase": "construction"},
                ),
            ]
        ),
    )
    assert session.scalar(select(Project)).phase == "construction"


def test_a_placeholder_is_not_disclosed_as_a_conflict(session):
    """A discarded claim is not a rival, and there was no contest to report.

    The note observed on #1 read `kept higher-weighted value` and named a URL that
    does not exist — describing a resolution that never happened on either count.
    """
    result = upsert_record(
        session,
        rec(
            sources=[
                placeholder_source(mw_planned=900.0),
                SourceRecord(
                    url="https://www.dcd.com/a",
                    source_type="trade_press",
                    fetched_at=T0,
                    claims={"mw_planned": 300.0},
                ),
            ]
        ),
    )
    project = session.get(Project, result.project_id)
    assert result.conflicts == []
    assert "conflict mw_planned" not in (project.notes or "")
    assert "PLACEHOLDER" not in (project.notes or "")


def test_a_placeholder_alone_still_populates_the_row(session):
    """Demoted, not dropped.

    `--allow-placeholders` exists so the shipped seed file can smoke-test the
    pipeline end to end. A claim-less source would make that produce empty rows,
    which is why these are weighted to zero rather than filtered out.
    """
    upsert_record(
        session,
        rec(
            sources=[
                placeholder_source(
                    name="Fairwater", company="Microsoft", city="Mount Pleasant", state="WI"
                )
            ]
        ),
    )
    project = session.scalar(select(Project))
    assert (project.name, project.city) == ("Fairwater", "Mount Pleasant")


def test_a_placeholder_does_not_get_to_name_an_identity_field(session):
    """FILL_ONLY takes `claims[0]`, so ordering *is* the policy here.

    On #1 the placeholder was source 1, created in the same second as the project,
    and its `company_filing` weight put it first — so "first seen, never
    overwritten" handed it every identity field. Sorting 待确认 last is what moves
    the real source into slot 0.

    Note what this test cannot show, and step 2 of the remediation plan therefore
    has to: once a placeholder has written an identity field, FILL_ONLY protects
    the stored value from every later source regardless. The demotion prevents the
    next one; it does not undo the ones already in the database.
    """
    upsert_record(
        session,
        rec(
            sources=[
                placeholder_source(county="Wrong County"),
                SourceRecord(
                    url="https://www.dcd.com/a",
                    source_type="general_media",
                    fetched_at=T0,
                    claims={"county": "Racine County"},
                ),
            ],
        ),
    )
    assert session.scalar(select(Project)).county == "Racine County"


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


# --- recompute_from_sources rewrites the notes it can regenerate -------------
#
# It used to compute the derived lines and throw them away, so a row re-derived by
# `tracker merge` or a `logic resolve` repair kept prose describing the claim set
# it held *before* the recompute.


def test_a_recompute_clears_a_disclosure_its_claims_no_longer_support(session):
    """The half of `_merge_notes`'s contract `recompute_from_sources` was missing."""
    upsert_record(
        session,
        rec(
            sources=[
                SourceRecord(
                    url="https://a.com/1",
                    source_type="trade_press",
                    fetched_at=T0,
                    claims={"mw_planned": 300.0},
                ),
                SourceRecord(
                    url="https://b.com/2",
                    source_type="company_filing",
                    fetched_at=T1,
                    claims={"mw_planned": 900.0},
                ),
            ]
        ),
    )
    project = session.scalar(select(Project))
    assert "conflict mw_planned" in project.notes

    # The disagreement goes away — as it would after a merge folded the rows, or
    # after an operator deleted the citation behind one of them.
    losing = next(s for s in project.sources if s.url == "https://a.com/1")
    session.delete(losing)
    session.flush()
    session.expire(project, ["sources"])
    recompute_from_sources(session, project)

    assert "conflict mw_planned" not in (project.notes or "")


def test_a_recompute_keeps_what_only_an_ingest_could_have_written(session):
    """The duplicate proposal is the *only* record that the question is open.

    `duplicate_of` is not a column — it is recomputed per ingest and disclosed in
    `notes`. Regenerating derived lines wholesale from a recompute, which has no
    ingest record in hand, would delete it silently.
    """
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
    )
    project = session.get(Project, result.project_id)
    assert "possible duplicate of project #" in project.notes

    recompute_from_sources(session, project)

    assert "possible duplicate of project #" in project.notes, (
        "a recompute must not erase the only record of an open identity question"
    )


def test_a_recompute_leaves_operator_prose_and_other_records_alone(session):
    upsert_record(session, rec(notes=["queue MW is generator nameplate"]))
    project = session.scalar(select(Project))
    project.notes = f"Spoke to the county planner.\n{project.notes}"
    session.flush()

    recompute_from_sources(session, project)

    assert "Spoke to the county planner." in project.notes
    assert "queue MW is generator nameplate" in project.notes


def test_the_preserved_note_prefixes_match_what_upsert_actually_writes(session):
    """`_INGEST_ONLY_NOTES` is matched against prose written somewhere else.

    Two string literals that have to agree and nothing forcing them to. If someone
    rewords either disclosure, the preservation silently stops working and a
    recompute starts deleting it again — so assert they still line up.
    """
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
    )
    notes = session.get(Project, result.project_id).notes.splitlines()
    derived = [line[len(NOTE_PREFIX) :].lstrip() for line in notes if line.startswith(NOTE_PREFIX)]
    matched = [d for d in derived if any(d.startswith(p) for p in _INGEST_ONLY_NOTES)]
    assert matched, f"no derived line matched {_INGEST_ONLY_NOTES}; got {derived}"


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


def test_a_verified_quote_upgrades_an_uncited_milestone(session):
    """How the 0017 backfill's `no_quote` rows get confirmed: a re-read under the
    new prompt arrives carrying the verified sentence, and the existing row takes
    it. This is the repair path for every pre-gate milestone in the database."""
    url = "https://news.microsoft.com/fairwater/"
    upsert_record(
        session,
        rec(
            sources=[manual_source(url)],
            events=[
                EventRecord(
                    dt.date(2023, 3, 1), "announced", "Announced.", url, unconfirmed="no_quote"
                )
            ],
        ),
    )
    upsert_record(
        session,
        rec(
            sources=[manual_source(url)],
            events=[
                EventRecord(
                    dt.date(2023, 3, 1),
                    "announced",
                    "Announced.",
                    url,
                    quote="Microsoft announced the project on March 1.",
                )
            ],
        ),
    )
    event = session.scalar(select(Event))
    assert event.unconfirmed is None
    assert event.quote == "Microsoft announced the project on March 1."


def test_a_later_uncited_read_does_not_strip_a_verified_quote(session):
    """The reverse must never happen: an article that merely fails to quote the
    milestone is not evidence against the sentence one already did."""
    url = "https://news.microsoft.com/fairwater/"
    upsert_record(
        session,
        rec(
            sources=[manual_source(url)],
            events=[
                EventRecord(
                    dt.date(2023, 3, 1),
                    "announced",
                    "Announced.",
                    url,
                    quote="Microsoft announced the project on March 1.",
                )
            ],
        ),
    )
    upsert_record(
        session,
        rec(
            sources=[manual_source(url)],
            events=[
                EventRecord(
                    dt.date(2023, 3, 1), "announced", "Announced.", url, unconfirmed="no_quote"
                )
            ],
        ),
    )
    event = session.scalar(select(Event))
    assert event.quote == "Microsoft announced the project on March 1."
    assert event.unconfirmed is None


def test_two_same_key_events_in_one_record_collapse_rather_than_crash(session):
    """One document can date two milestones of the same kind to the same day.

    Migration 0002 accepts that they collapse into one row. What it must not do is
    fail: the dedup map is built from what is already stored, so without
    registering each insert as it is made, both are added and the flush dies on
    uq_event_project_type_date — taking the whole run down, not just the record.

    Seen live on an SEC filing listing two capacity expansions dated the same day.
    A news article rarely does, which is why this survived until filings became a
    source.
    """
    url = "https://www.sec.gov/Archives/edgar/data/1/2/f.htm"
    result = upsert_record(
        session,
        rec(
            sources=[manual_source(url)],
            events=[
                EventRecord(dt.date(2026, 4, 30), "expanded", "AMD exercised 100 MW option.", url),
                EventRecord(dt.date(2026, 4, 30), "expanded", "A second expansion, same day.", url),
            ],
        ),
    )
    assert result.events_written == 1
    assert session.scalar(select(func.count()).select_from(Event)) == 1
    # The later one wins, consistent with how a re-ingest updates the description.
    assert session.scalar(select(Event)).description == "A second expansion, same day."


def test_two_same_key_risks_in_one_record_collapse_rather_than_crash(session):
    """The same guard on the risk path, which `ingest manual` can reach."""
    url = "https://www.sec.gov/Archives/edgar/data/1/2/f.htm"
    result = upsert_record(
        session,
        rec(
            sources=[manual_source(url)],
            risks=[
                RiskRecord("permitting", "watch", "First.", first_seen=None, source_url=url),
                RiskRecord("permitting", "material", "Second.", first_seen=None, source_url=url),
            ],
        ),
    )
    assert result.risks_written == 1
    assert session.scalar(select(func.count()).select_from(Risk)) == 1


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


def test_existing_only_refuses_to_create_a_project(session):
    """The guard a re-read needs, and the exact opposite of `force_new`.

    Re-reading Hyperion's own articles with today's instructions also yields
    "Project Everest" — a real name in those articles, and not a campus this
    database has decided to track. A repair pass that quietly adds rows is no
    longer a repair; it is an ingest with no worklist and no review.
    """
    result = upsert_record(session, rec(company="Meta", name="Everest"), existing_only=True)

    assert result.action == "refused"
    assert result.project_id == 0
    # Nothing at all was written — not the project, and not its citations either.
    assert session.scalar(select(func.count()).select_from(Project)) == 0
    assert session.scalar(select(func.count()).select_from(Source)) == 0


def test_existing_only_still_updates_a_project_that_exists(session):
    """Refusing to create is not refusing to work."""
    upsert_record(session, rec())
    result = upsert_record(session, rec(sources=[manual_source(mw_planned=250.0)]), existing_only=True)

    assert result.action in {"update", "unchanged"}
    assert session.scalar(select(Project)).mw_planned == 250.0


def test_the_rationale_names_the_risk_the_column_actually_holds(session):
    """The explanation and the choice share `choose_blocker`, so they cannot part.

    An explanation free to name a different obstacle than the one on the row would
    be worse than none: a reader would act on the wrong sentence and have no way to
    tell.
    """
    from tracker.upsert import blocker_rationale

    upsert_record(
        session,
        rec(
            risks=[
                risk(category="water", severity="watch", summary="Draw questioned."),
                risk(category="permitting", severity="blocking", summary="Rezoning refused."),
            ]
        ),
    )
    project = session.scalar(select(Project))
    why = blocker_rationale(project)

    assert why["summary"] == project.blocker
    assert why["category"] == "permitting"
    assert why["considered"] == 2
    assert why["arbitrary"] is False
    assert "most severe of 2 open obstacles" in why["why"]


def test_the_rationale_admits_when_the_choice_was_arbitrary(session):
    """Two obstacles ranking equally are settled on the lowest row id.

    Stable across runs, which is what it was for — and meaningless as a reason,
    which is what the reader has to be told. A confident sentence here would hide
    exactly the case worth knowing about.
    """
    from tracker.upsert import blocker_rationale

    upsert_record(
        session,
        rec(
            risks=[
                risk(category="permitting", severity="blocking", summary="Rezoning refused."),
                risk(category="water", severity="blocking", summary="Aquifer draw refused."),
            ]
        ),
    )
    why = blocker_rationale(session.scalar(select(Project)))

    assert why["tied"] == 2
    assert why["arbitrary"] is True
    assert "lowest row id" in why["why"]


def test_there_is_no_rationale_without_an_obstacle(session):
    from tracker.upsert import blocker_rationale

    upsert_record(session, rec())
    assert blocker_rationale(session.scalar(select(Project))) is None


def test_an_unconfirmed_risk_is_stored_with_its_reason(session):
    result = upsert_record(session, rec(risks=[risk(unconfirmed="no_quote")]))
    assert result.risks_written == 1
    row = session.scalar(select(Risk))
    assert row.unconfirmed == "no_quote"
    assert row.quote is None


def test_a_confirmed_risk_outranks_an_unconfirmed_one_for_the_blocker(session):
    """`blocker` is a tracked field, so what fills it must be the best evidenced.

    Severity alone would let a 待确认 `blocking` obstacle displace a quoted
    `material` one, putting an unevidenced sentence in the twelve-field count.
    """
    upsert_record(
        session,
        rec(
            risks=[
                risk(
                    category="permitting",
                    severity="blocking",
                    summary="Rezoning refused.",
                    unconfirmed="no_quote",
                ),
                risk(
                    category="transmission",
                    severity="material",
                    summary="Upgrades pending.",
                    quote="two 345-kilovolt upgrades",
                ),
            ]
        ),
    )
    assert session.scalar(select(Project)).blocker == "Upgrades pending."


def test_an_unconfirmed_reread_does_not_demote_an_evidenced_risk(session):
    """Two sources report one obstacle and only one quotes it usably.

    The citation is the thing worth keeping. Letting the later read overwrite it
    would mean a refresh could silently strip the evidence off an obstacle that
    had it, which is the opposite of what re-reading is for.
    """
    upsert_record(session, rec(risks=[risk(quote="two 345-kilovolt upgrades")]))
    upsert_record(session, rec(risks=[risk(summary="Still waiting.", unconfirmed="no_quote")]))

    row = session.scalar(select(Risk))
    assert row.quote == "two 345-kilovolt upgrades"
    assert row.unconfirmed is None
    assert row.summary == "Still waiting.", "the wording still refreshes"


def test_a_confirmed_reread_promotes_an_unconfirmed_risk(session):
    """And the other direction, which is the point of re-reading at all."""
    upsert_record(session, rec(risks=[risk(unconfirmed="no_quote")]))
    upsert_record(session, rec(risks=[risk(quote="two 345-kilovolt upgrades")]))

    row = session.scalar(select(Risk))
    assert row.unconfirmed is None
    assert row.quote == "two 345-kilovolt upgrades"


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


# --- the 待确认 tier ----------------------------------------------------------


def unconfirmed_source(url="https://news.microsoft.com/fairwater/", *, mark=(), **claims):
    """A citation where `mark` names the claims no quote supports."""
    base = {"name": "Fairwater", "company": "Microsoft", "city": "Mount Pleasant", "state": "WI"}
    base.update(claims)
    return SourceRecord(
        url=url,
        source_type=claims.pop("source_type", None) or "manual",
        fetched_at=T0,
        excerpt="A quote.",
        claims=base,
        unconfirmed=frozenset(mark),
    )


def test_an_unconfirmed_value_is_stored_but_not_counted_as_a_fact(session):
    """The PRD's third outcome: mark it, do not guess, do not delete it.

    `source.fields` is what `confidence`, the traceability test and the 9-of-12
    count all read, so an unconfirmed value must be absent from it while still
    reaching the project row.
    """
    upsert_record(
        session,
        rec(
            sources=[
                unconfirmed_source(
                    mw_planned=900.0, investment_usd=5_000_000_000, mark=("investment_usd",)
                )
            ]
        ),
    )
    project = session.scalars(select(Project)).one()
    source = project.sources[0]

    assert project.investment_usd == 5_000_000_000, "kept, not destroyed"
    assert "investment_usd" not in (source.fields or ""), "must not count as a fact"
    assert "investment_usd" in (source.unconfirmed_fields or ""), "must be flagged"
    assert "mw_planned" in (source.fields or "")


def test_a_confirmed_value_always_beats_an_unconfirmed_one(session):
    """Whatever the other source's weight or recency."""
    weak = SourceRecord(
        url="https://weak.example/a",
        source_type="general_media",
        fetched_at=T0,
        claims={
            "name": "Fairwater",
            "company": "Microsoft",
            "city": "Mount Pleasant",
            "state": "WI",
            "mw_planned": 100.0,
        },
    )
    strong = SourceRecord(
        url="https://strong.example/b",
        source_type="company_filing",
        fetched_at=T0,
        claims={
            "name": "Fairwater",
            "company": "Microsoft",
            "city": "Mount Pleasant",
            "state": "WI",
            "mw_planned": 999.0,
        },
        unconfirmed=frozenset({"mw_planned"}),
    )
    upsert_record(session, rec(sources=[weak]))
    upsert_record(session, rec(sources=[strong]))

    project = session.scalars(select(Project)).one()
    assert project.mw_planned == 100.0, "a quoted value outranks an unquoted one"


def test_an_unconfirmed_value_cannot_win_a_max_policy_either(session):
    """MAX/MIN/PHASE scan every claim, so the filter cannot live in one policy."""
    upsert_record(session, rec(sources=[unconfirmed_source(mw_built=10.0)]))
    upsert_record(
        session,
        rec(
            sources=[
                unconfirmed_source("https://other.example/b", mw_built=9999.0, mark=("mw_built",))
            ]
        ),
    )
    project = session.scalars(select(Project)).one()
    assert project.mw_built == 10.0, "MAX must ignore the unconfirmed higher value"


def test_an_unconfirmed_value_fills_a_field_nothing_else_covers(session):
    """The whole point: a flagged candidate beats a hole."""
    upsert_record(
        session,
        rec(sources=[unconfirmed_source(expected_online="2027-01-01", mark=("expected_online",))]),
    )
    project = session.scalars(select(Project)).one()
    assert project.expected_online is not None


def test_an_unconfirmed_value_does_not_raise_confidence(session):
    """Otherwise a guess would buy the trust a quote earns.

    Both upserts use the SAME url, so no second domain is introduced and the only
    variable is the unconfirmed claim.
    """
    upsert_record(session, rec(sources=[unconfirmed_source(mw_planned=900.0)]))
    quoted_only = session.scalars(select(Project)).one().confidence

    upsert_record(
        session,
        rec(
            sources=[
                unconfirmed_source(mw_planned=900.0, investment_usd=1, mark=("investment_usd",))
            ]
        ),
    )
    with_a_guess = session.scalars(select(Project)).one().confidence
    assert with_a_guess <= quoted_only, "an unquoted claim must not buy confidence"


def test_a_citation_supporting_only_guesses_does_not_corroborate(session):
    """A second URL full of 待确认 values must not lift a project from 2 to 3."""
    upsert_record(session, rec(sources=[unconfirmed_source(mw_planned=900.0)]))
    one_source = session.scalars(select(Project)).one().confidence

    all_guessed = SourceRecord(
        url="https://guesses.example/x",
        source_type="company_filing",
        fetched_at=T0,
        claims={"investment_usd": 7, "expected_online": "2030-01-01"},
        unconfirmed=frozenset({"investment_usd", "expected_online"}),
    )
    upsert_record(session, rec(sources=[all_guessed]))
    after = session.scalars(select(Project)).one().confidence
    assert after <= one_source, "a citation supporting nothing corroborates nothing"


def test_a_recompute_lets_phase_come_back_down(session):
    """`phase` used to be a one-way ratchet, so a bad value was permanent.

    Hyperion (#10) read `operational` on the strength of a single Instagram post
    that the evidence gate had already marked 待确认. `resolve` discards unconfirmed
    claims whenever any confirmed claim exists, so the merge engine returned
    `construction` from the claims — and the row still said `operational`, because
    `_resolve_ladder` folded the stored value into the comparison and nothing could
    ever lower it.

    Neither write path ratchets — `upsert_record` and `recompute_from_sources` both
    pass `ratchet=False`, because each derives the row from the complete set of
    citations rather than from whatever the row happened to be carrying. The ratchet
    is on by default for the *read* paths only.
    """
    import json

    from tracker.models import Project, Source
    from tracker.upsert import recompute_from_sources

    row = Project(
        name="Ratchet",
        company="Meta",
        city="Rayville",
        state="LA",
        dedup_key="meta|ratchet",
        phase="operational",
    )
    session.add(row)
    session.flush()
    session.add(
        Source(
            project=row,
            url="https://ratchet.test/1",
            source_type="company_filing",
            fetched_at=dt.datetime(2026, 1, 1),
            excerpt="an excerpt",
            fields="phase",
            claims=json.dumps({"phase": "construction"}),
            quotes=json.dumps({"phase": "construction is under way"}),
        )
    )
    session.flush()

    recompute_from_sources(session, row)
    assert row.phase == "construction"


def test_the_phase_ratchet_is_the_default_and_can_be_turned_off(session):
    """On by default for the read paths; off in both writers."""
    from tracker.upsert import Policy, _Claim, resolve

    claims = [_Claim("announced", 1, dt.datetime(2026, 1, 1), "general_media", "u")]
    assert resolve(Policy.PHASE, claims, "operational") == "operational"
    assert resolve(Policy.PHASE, claims, "operational", ratchet=False) == "announced"


def test_the_ratchet_governs_max_and_min_too(session):
    """It was threaded into PHASE only, so MAX could not fall and MIN could not rise.

    The flag was a lie for the two other policies that scan the whole claim set: the
    stored value was appended to the candidates unconditionally, so it was always one
    of its own rivals and always won.
    """
    from tracker.upsert import Policy, _Claim, resolve

    built = [_Claim(200.0, 2, dt.datetime(2026, 1, 1), "trade_press", "u")]
    assert resolve(Policy.MAX, built, 1200.0) == 1200.0
    assert resolve(Policy.MAX, built, 1200.0, ratchet=False) == 200.0

    announced = [_Claim("2024-03-01", 2, dt.datetime(2026, 1, 1), "trade_press", "u")]
    assert resolve(Policy.MIN, announced, dt.date(2019, 1, 1)) == "2019-01-01"
    assert resolve(Policy.MIN, announced, dt.date(2019, 1, 1), ratchet=False) == "2024-03-01"


def test_turning_the_ratchet_off_does_not_make_a_field_clearable(session):
    """`DERIVED_FIELDS` rests on this: no usable candidate returns what is stored.

    The merge loop writes `chosen` straight through, so returning None here would
    NULL a column on the strength of an unreadable claim — which is exactly why
    `blocker` had to leave the loop rather than be given a policy. Only a rival the
    policy can actually compare may lower a MAX field.
    """
    from tracker.upsert import Policy, _Claim, resolve

    assert resolve(Policy.MAX, [], 1200.0, ratchet=False) == 1200.0
    prose = [_Claim("about 200 MW", 2, dt.datetime(2026, 1, 1), "trade_press", "u")]
    assert resolve(Policy.MAX, prose, 1200.0, ratchet=False) == 1200.0


def test_a_claim_a_decision_ruled_against_leaves_the_merge(session):
    """Superseding the *only* claim must not hand the figure straight back.

    The 待确认 filter is conditional on a confirmed rival existing, because an
    unquoted value still beats nothing. A decision is the other question: once a
    human or the solver rules a figure out, "no source we trust states this" has to
    mean the field is empty, not that the ruled-out figure returns as a last resort.
    That was why `audit`'s clear-the-capacity action never stuck.
    """
    import json

    from tracker.conflicts import supersede
    from tracker.models import Project, Source
    from tracker.upsert import recompute_from_sources

    row = Project(
        name="Ruled",
        company="Meta",
        city="Rayville",
        state="LA",
        dedup_key="meta|ruled",
        mw_planned=36000.0,
    )
    session.add(row)
    session.flush()
    source = Source(
        project=row,
        url="https://ruled.test/1",
        source_type="company_filing",
        fetched_at=dt.datetime(2026, 1, 1),
        excerpt="an excerpt",
        fields="mw_planned",
        claims=json.dumps({"mw_planned": 36000.0}),
        quotes=json.dumps({"mw_planned": "36,000 MW at full buildout"}),
    )
    session.add(source)
    session.flush()

    assert supersede(source, "mw_planned") is True
    row.mw_planned = None
    session.flush()
    recompute_from_sources(session, row)
    assert row.mw_planned is None
    # The claim itself is untouched — the article still said what it said.
    assert json.loads(source.claims)["mw_planned"] == 36000.0


def test_a_recompute_lets_a_max_field_come_back_down(session):
    """`mw_built` was a one-way ratchet for the same reason `phase` was.

    Stargate Abilene (#3) read `mw_built = 1200` while the only claim on the row was
    a well-quoted 200: both "1.2 GW" quotes had been re-extracted as `mw_planned`,
    correctly — committed capacity is not energised capacity — and MAX counted the
    stored figure among its own candidates, so nothing could lower it. The figure
    outlived the claim that produced it by 1,000 MW.
    """
    import json

    from tracker.models import Project, Source
    from tracker.upsert import recompute_from_sources

    row = Project(
        name="Abilene",
        company="Oracle",
        city="Abilene",
        state="TX",
        dedup_key="oracle|abilene",
        mw_built=1200.0,
    )
    session.add(row)
    session.flush()
    session.add(
        Source(
            project=row,
            url="https://abilene.test/1",
            source_type="trade_press",
            fetched_at=dt.datetime(2026, 1, 1),
            excerpt="an excerpt",
            fields="mw_built",
            claims=json.dumps({"mw_built": 200.0}),
            quotes=json.dumps({"mw_built": "200 MW is energised and serving"}),
        )
    )
    session.flush()

    recompute_from_sources(session, row)
    assert row.mw_built == 200.0


def test_a_recompute_lets_first_announced_move_later(session):
    """The MIN half of the same defect: a date earlier than anything cited.

    `first_announced` means the first announcement anybody saw, so MIN is right — but
    the row's own date was one of the candidates, which made a wrong-early value as
    permanent as a wrong-high capacity.
    """
    import json

    from tracker.models import Project, Source
    from tracker.upsert import recompute_from_sources

    row = Project(
        name="Early",
        company="Oracle",
        city="Abilene",
        state="TX",
        dedup_key="oracle|early",
        first_announced=dt.date(2019, 1, 1),
    )
    session.add(row)
    session.flush()
    session.add(
        Source(
            project=row,
            url="https://early.test/1",
            source_type="company_filing",
            fetched_at=dt.datetime(2026, 1, 1),
            excerpt="an excerpt",
            fields="first_announced",
            claims=json.dumps({"first_announced": "2024-03-01"}),
            quotes=json.dumps({"first_announced": "announced in March 2024"}),
        )
    )
    session.flush()

    recompute_from_sources(session, row)
    assert row.first_announced == dt.date(2024, 3, 1)
    assert isinstance(row.first_announced, dt.date)
