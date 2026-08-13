"""Publisher ranking derived from what claims actually decided.

The load-bearing test here is `test_credits_the_policy_winner_not_the_strongest_source`.
Four of the twelve tracked fields do not take the head of the sorted claim list —
`mw_built` takes the MAX, `first_announced` the MIN, `phase` the furthest rung —
so attributing by sort order credits the wrong publisher on a third of the schema.
If that test passes, this module is asking the write path rather than guessing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker.ingest.records import IngestRecord, SourceRecord
from tracker.sources import (
    IDENTITY_FIELDS,
    SCORED_FIELDS,
    Survey,
    decisive_by_source,
    host_of,
    is_publisher,
    survey,
)
from tracker.upsert import upsert_record

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)
T1 = dt.datetime(2026, 6, 1, 12, 0, 0)


def src(url, *, source_type="trade_press", fetched_at=T0, extractor=None, **claims):
    base = {"name": "Hyperion", "company": "Meta", "county": "Richland Parish", "state": "LA"}
    base.update(claims)
    return SourceRecord(
        url=url,
        source_type=source_type,
        fetched_at=fetched_at,
        excerpt="A quote.",
        claims=base,
        extractor=extractor,
    )


def project(*sources):
    return IngestRecord(
        project={
            "company": "Meta",
            "name": "Hyperion",
            "city": None,
            "county": "Richland Parish",
            "state": "LA",
            "country": "US",
        },
        sources=list(sources),
    )


def sources_of(session, project_id=1):
    from sqlalchemy import select

    from tracker.models import Source

    return list(session.scalars(select(Source).where(Source.project_id == project_id)))


# --- host_of ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.datacenterfrontier.com/a/b", "datacenterfrontier.com"),
        ("https://datacenterfrontier.com/a", "datacenterfrontier.com"),
        # Subdomains collapse, because `confidence.independent_domains` counts them
        # as one party. Ranking them separately would make a host one source for
        # corroboration and two for reputation.
        ("https://ir.applieddigital.com/x", "applieddigital.com"),
        ("", "(no host)"),
        (None, "(no host)"),
    ],
)
def test_host_of_matches_the_independence_notion_of_a_publisher(url, expected):
    assert host_of(url) == expected


# --- what counts as a publisher ---------------------------------------------


def test_derived_reference_rows_are_not_publishers(session):
    """Census geography is cited like a source but publishes nothing.

    Ranking it produced `www2.census.gov: 184 cited, 184 inert, 0 decisive` — the
    worst publisher in the database, for a lookup table.
    """
    upsert_record(
        session,
        project(
            src("https://www.datacenterfrontier.com/a", mw_planned=5000.0),
            src("https://www2.census.gov/geo/x", extractor="derived:geo-v1"),
        ),
    )
    session.flush()
    rows = sources_of(session)
    assert [is_publisher(s) for s in rows] == [True, False]

    result = survey(session)
    assert result.skipped == 1
    assert "census.gov" not in {h.host for h in result.hosts}


def test_placeholder_citations_are_not_publishers(session):
    from tracker.confidence import PLACEHOLDER_MARKER

    upsert_record(
        session,
        project(src(f"https://example.test/{PLACEHOLDER_MARKER}/x", mw_planned=1.0)),
    )
    session.flush()
    assert [is_publisher(s) for s in sources_of(session)] == [False]


# --- attribution ------------------------------------------------------------


def test_identity_fields_are_never_scored():
    """`name` and `company` are FILL_ONLY: a win on one records crawl order."""
    assert {"name", "company", "city", "state"} == IDENTITY_FIELDS
    assert not IDENTITY_FIELDS & set(SCORED_FIELDS)
    assert "mw_planned" in SCORED_FIELDS
    assert len(SCORED_FIELDS) == 8


def test_credits_the_policy_winner_not_the_strongest_source(session):
    """`mw_built` is MAX, so the heaviest source loses to a bigger figure.

    A `company_filing` (weight 3) claiming 100 MW built sorts above a
    `general_media` (weight 1) claiming 400, but MAX takes 400 — so the credit
    belongs to the weaker source. Attributing by `claims[0]` gets this backwards.
    """
    upsert_record(
        session,
        project(
            src("https://filing.test/a", source_type="company_filing", mw_built=100.0),
            src("https://blog.test/b", source_type="general_media", mw_built=400.0),
        ),
    )
    session.flush()
    rows = sources_of(session)
    by_url = {s.url: s.id for s in rows}
    won = decisive_by_source(rows).won

    strongest = by_url["https://filing.test/a"]
    bigger = by_url["https://blog.test/b"]
    assert "mw_built" not in won.get(strongest, {})
    assert won[bigger]["mw_built"] == 1


def test_an_unopposed_win_is_not_contested(session):
    upsert_record(session, project(src("https://only.test/a", mw_planned=5000.0)))
    session.flush()
    rows = sources_of(session)
    attribution = decisive_by_source(rows)
    sid = rows[0].id
    assert attribution.won[sid]["mw_planned"] == 1
    assert "mw_planned" not in attribution.contested.get(sid, {})


def test_a_win_over_a_disagreeing_rival_is_contested(session):
    upsert_record(
        session,
        project(
            src("https://strong.test/a", source_type="company_filing", mw_planned=5000.0),
            src("https://weak.test/b", source_type="general_media", mw_planned=14462.0),
        ),
    )
    session.flush()
    rows = sources_of(session)
    winner = next(s for s in rows if "strong" in s.url)
    attribution = decisive_by_source(rows)
    assert attribution.contested[winner.id]["mw_planned"] == 1


def test_agreement_within_tolerance_is_not_a_conflict(session):
    """Uses `confidence.values_conflict`, so 2000 and 2000.0 are one figure.

    Without sharing that function this report would call a float/int round-trip a
    disagreement, and `contested` would count JSON serialisation as a dispute.
    """
    upsert_record(
        session,
        project(
            src("https://a.test/a", source_type="company_filing", mw_planned=2000),
            src("https://b.test/b", source_type="general_media", mw_planned=2000.0),
        ),
    )
    session.flush()
    rows = sources_of(session)
    winner = next(s for s in rows if "a.test" in s.url)
    assert decisive_by_source(rows).contested.get(winner.id, {}).get("mw_planned") is None


def test_a_source_that_only_restates_identity_is_inert(session):
    """The restatement noise: cited, and contributed no fact anybody was missing."""
    upsert_record(
        session,
        project(
            src("https://real.test/a", mw_planned=5000.0),
            src("https://echo.test/b"),  # identity fields only
        ),
    )
    session.flush()
    result = survey(session)
    echo = next(h for h in result.hosts if h.host == "echo.test")
    real = next(h for h in result.hosts if h.host == "real.test")
    assert (echo.cited, echo.decisive, echo.inert) == (1, 0, 1)
    assert real.decisive == 1
    assert real.inert == 0


# --- survey totals ----------------------------------------------------------


def test_every_citation_is_accounted_for(session):
    """`cited == decisive_sources + contributing + inert`, by construction.

    The identity is the audit: a host whose columns do not add up is a bug in this
    module rather than a judgement call, so it is asserted rather than trusted.
    """
    upsert_record(
        session,
        project(
            src("https://a.test/1", source_type="company_filing", mw_planned=5000.0),
            src("https://b.test/1", source_type="general_media", mw_planned=14462.0),
            src("https://c.test/1"),
        ),
    )
    session.flush()
    result = survey(session)
    assert result.hosts
    for host in result.hosts:
        assert host.adds_up(), host
    assert sum(h.cited for h in result.hosts) == result.sources_read


def test_survey_of_an_empty_database_is_empty(session):
    result = survey(session)
    assert result.hosts == []
    assert result.as_json()["hosts"] == []


# --- ranking ----------------------------------------------------------------


def _stat(host, cited, decisive, contested=0):
    from tracker.sources import HostStat

    return HostStat(host=host, cited=cited, decisive=decisive, contested=contested)


def test_decisive_ordering_is_raw_usage_and_ranks_every_host():
    little = _stat("lucky.test", cited=1, decisive=8)
    lots = _stat("frontier.test", cited=267, decisive=240)
    ranked = Survey(hosts=[little, lots]).ranked(by="decisive")
    assert [h.host for h in ranked] == ["frontier.test", "lucky.test"]


def test_yield_ordering_excludes_hosts_below_the_citation_floor():
    """The bug this floor exists for.

    A host cited once on a single-source project wins every field unopposed and
    scores a ratio no real outlet can reach. The first version of this report
    ranked eight `.gov` pages cited once apiece above every trade outlet.
    """
    lucky = _stat("lucky.test", cited=1, decisive=8)
    proven = _stat("frontier.test", cited=267, decisive=240)
    ranked = Survey(hosts=[lucky, proven]).ranked(by="yield")
    assert [h.host for h in ranked] == ["frontier.test"]

    # Explicitly asking for no floor puts it back, so the caller can see it.
    ranked = Survey(hosts=[lucky, proven]).ranked(by="yield", min_cited=0)
    assert ranked[0].host == "lucky.test"


def test_contested_ordering_prefers_wins_against_a_rival():
    quiet = _stat("quiet.test", cited=50, decisive=60, contested=1)
    fought = _stat("fought.test", cited=50, decisive=40, contested=30)
    ranked = Survey(hosts=[quiet, fought]).ranked(by="contested")
    assert [h.host for h in ranked] == ["fought.test", "quiet.test"]


def test_ordering_is_total_so_output_diffs_between_runs():
    a = _stat("a.test", cited=10, decisive=5)
    b = _stat("b.test", cited=10, decisive=5)
    first = [h.host for h in Survey(hosts=[a, b]).ranked()]
    second = [h.host for h in Survey(hosts=[b, a]).ranked()]
    assert first == second == ["a.test", "b.test"]


def test_an_unknown_ordering_is_refused():
    with pytest.raises(ValueError, match="unknown ordering"):
        Survey(hosts=[]).ranked(by="popularity")


def test_type_weight_reports_the_hand_set_table(session):
    upsert_record(
        session,
        project(src("https://dcf.test/a", source_type="trade_press", mw_planned=5000.0)),
    )
    session.flush()
    host = next(h for h in survey(session).hosts if h.host == "dcf.test")
    assert host.type_weight == 2
    assert host.types["trade_press"] == 1
