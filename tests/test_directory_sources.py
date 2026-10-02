"""Directories, wikis and digests fill gaps; they never displace a first-hand report.

A facility directory is `general_media` like any local paper, so before this rule it
tied with one on weight and won on recency — and a listing is always "recent",
because it is re-crawled whenever anyone looks. The hand audit of 2026-10-01 traced
a wrong campus capacity to a directory page that gave one building's figures for
the whole site. What is pinned here is the rule's shape: which filter comes first,
which policies it reaches, and that every reader reporting a contest applies it.
"""

from __future__ import annotations

import datetime as dt

from tracker import confidence, conflicts
from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.models import Project
from tracker.upsert import _Claim, contenders, upsert_record

OLD = dt.datetime(2025, 3, 1, 12, 0)
NEW = dt.datetime(2026, 9, 25, 12, 0)

REPORT = "https://www.localpaper.example/news/campus-approved"
DIRECTORY = "https://servercountry.org/data/projects/example-campus"
OTHER_DIRECTORY = "https://dchub.cloud/facilities/example-campus"


def _cite(url, *, when, unquoted=(), **claims):
    return SourceRecord(
        url=url,
        source_type="general_media",
        excerpt="excerpt",
        claims=claims,
        quotes={k: f"the campus is {v}" for k, v in claims.items()},
        unconfirmed=frozenset(unquoted),
        fetched_at=when,
    )


def _campus(session, *sources) -> Project:
    result = upsert_record(
        session,
        IngestRecord(
            project={"company": "Example", "name": "Example Campus", "city": "Ames", "state": "IA"},
            sources=list(sources),
        ),
    )
    session.commit()
    return session.get(Project, result.project_id)


def test_the_list_covers_the_directories_the_audit_found():
    for url in (DIRECTORY, OTHER_DIRECTORY, "https://epoch.ai/data/ai-data-centers/x"):
        assert confidence.is_tertiary(confidence.SourceView(url=url, source_type="general_media"))
    assert not confidence.is_tertiary(
        confidence.SourceView(url=REPORT, source_type="general_media")
    )


def test_an_older_report_outranks_a_newer_directory(session):
    """Same weight, and the directory was crawled last: it used to win on recency."""
    project = _campus(
        session,
        _cite(REPORT, when=OLD, mw_planned=300.0),
        _cite(DIRECTORY, when=NEW, mw_planned=450.0),
    )
    assert project.mw_planned == 300.0


def test_a_directory_fills_a_field_nothing_first_hand_states(session):
    project = _campus(
        session,
        _cite(REPORT, when=OLD, mw_planned=300.0),
        _cite(DIRECTORY, when=NEW, investment_usd=2_000_000_000),
    )
    assert project.mw_planned == 300.0
    assert project.investment_usd == 2_000_000_000


def test_the_rule_reaches_policies_that_scan_every_claim(session):
    """`mw_built` takes the largest figure, so a filter applied only to the ordering
    would have let a directory's bigger number win anyway."""
    project = _campus(
        session,
        _cite(REPORT, when=OLD, mw_built=200.0, phase="construction"),
        _cite(DIRECTORY, when=NEW, mw_built=340.0, phase="operational"),
    )
    assert project.mw_built == 200.0
    assert project.phase == "construction"


def test_a_quoted_directory_figure_still_beats_an_unquoted_report(session):
    """The quote rule comes first: an unquoted value is not evidence of anything yet."""
    project = _campus(
        session,
        _cite(REPORT, when=OLD, unquoted={"mw_planned"}, mw_planned=300.0),
        _cite(DIRECTORY, when=NEW, mw_planned=450.0),
    )
    assert project.mw_planned == 450.0


def test_contenders_applies_the_filters_in_order():
    def claim(value, *, confirmed=True, tertiary=False, decided=False):
        return _Claim(
            value,
            1,
            OLD,
            "general_media",
            f"https://x.example/{value}",
            confirmed=confirmed,
            decided_against=decided,
            tertiary=tertiary,
        )

    report, listing = claim(300), claim(450, tertiary=True)
    assert contenders([listing, report]) == [report]
    assert contenders([listing]) == [listing]
    ruled = claim(300, decided=True)
    assert contenders([ruled, listing]) == [listing]
    unquoted = claim(300, confirmed=False)
    assert contenders([unquoted, listing]) == [listing]


def test_a_report_against_a_directory_is_not_a_dispute(session):
    """The rule settled it, so a model asked to choose would be paid to repeat it."""
    project = _campus(
        session,
        _cite(REPORT, when=OLD, mw_planned=300.0),
        _cite(DIRECTORY, when=NEW, mw_planned=450.0),
    )
    assert [d for d in conflicts.disputes(project) if d.field == "mw_planned"] == []
    assert "conflict mw_planned" not in (project.notes or "")


def test_two_directories_with_nothing_first_hand_are_still_a_dispute(session):
    project = _campus(
        session,
        _cite(DIRECTORY, when=OLD, mw_planned=300.0),
        _cite(OTHER_DIRECTORY, when=NEW, mw_planned=450.0),
    )
    (dispute,) = [d for d in conflicts.disputes(project) if d.field == "mw_planned"]
    assert {o.value for o in dispute.options} == {300.0, 450.0}


def test_the_reason_given_names_the_rule(session):
    from tracker.logic import check_collisions

    project = _campus(
        session,
        _cite(REPORT, when=OLD, mw_planned=300.0),
        _cite(DIRECTORY, when=NEW, mw_planned=450.0),
    )
    (collision,) = [c for c in check_collisions(project) if c.field == "mw_planned"]
    assert collision.decided_by == "first-hand"
    assert "directory" in collision.why
    assert collision.winner == 300.0


def test_the_export_marks_a_directory_claim(session):
    from tracker.export import _claims_json

    project = _campus(
        session,
        _cite(REPORT, when=OLD, mw_planned=300.0),
        _cite(DIRECTORY, when=NEW, mw_planned=450.0),
    )
    claims = _claims_json(project)["mw_planned"]["claims"]
    assert [(c["value"], c["tertiary"], c["is_winner"]) for c in claims] == [
        (300.0, False, True),
        (450.0, True, False),
    ]


def test_a_directory_never_corroborates_the_report_it_copied():
    report = confidence.SourceView(
        url=REPORT, source_type="general_media", claims={"mw_planned": 300.0}, fields="mw_planned"
    )
    listing = confidence.SourceView(
        url=DIRECTORY,
        source_type="general_media",
        claims={"mw_planned": 300.0},
        fields="mw_planned",
    )
    assert confidence.compute([report]).value == confidence.compute([report, listing]).value
