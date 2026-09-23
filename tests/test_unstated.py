"""A figure no citation states any more is cleared, and says so.

The merge used to hand back whatever the row held whenever no claim on a field took
part. That is the policy for identity fields and harmless for derived ones; for the
facts a reader sums it let a value outlive every claim that ever stated it. On the
production copy this was fixed against, 68 values stood that way — $487B of
investment among them, $450B of it one Michigan campus carrying the whole Stargate
programme's figure after a better prompt had stopped attributing it there.
"""

from __future__ import annotations

import datetime as dt
import json

from tracker.audit import settled_codes
from tracker.models import Project, Source
from tracker.upsert import CLAIM_OWNED_FIELDS, DEFAULT_PHASE, recompute_from_sources

T0 = dt.datetime(2026, 1, 10, 12, 0, 0)


def _row(session, **fields) -> Project:
    defaults = {
        "name": "Saline Township campus",
        "company": "OpenAI",
        "city": "Saline",
        "state": "MI",
        "dedup_key": "openai|city:saline|MI",
        "phase": "announced",
        "confidence": 1,
    }
    project = Project(**{**defaults, **fields})
    session.add(project)
    session.flush()
    return project


def _cite(session, project, url: str, reasons: dict | None = None, **claims) -> Source:
    source = Source(
        project_id=project.id,
        url=url,
        source_type="trade_press",
        fetched_at=T0,
        claims=json.dumps(claims),
        fields=",".join(sorted(claims)),
        unconfirmed_reasons=json.dumps(reasons) if reasons else None,
    )
    session.add(source)
    session.flush()
    session.refresh(project)
    return source


def test_a_figure_its_re_read_article_no_longer_claims_is_cleared(session):
    """The #237 shape: the only article, re-read by a better prompt, no longer
    attributes the programme's $450B to this campus."""
    project = _row(session, investment_usd=450_000_000_000, mw_planned=1000.0)
    _cite(session, project, "https://example.test/saline", mw_planned=1000.0)

    recompute_from_sources(session, project)

    assert project.investment_usd is None
    assert project.mw_planned == 1000.0, "a figure that is still claimed stays"
    assert "investment_usd 450,000,000,000 -> empty (no citation" in project.notes


def test_every_claim_ruled_out_leaves_the_field_empty(session):
    """#98: 15.5 MW built with both of its claims superseded."""
    project = _row(session, mw_built=80.0)
    _cite(session, project, "https://example.test/a", {"mw_built": "superseded"}, mw_built=80.0)
    _cite(session, project, "https://example.test/b", {"mw_built": "misread"}, mw_built=13.0)

    recompute_from_sources(session, project)
    assert project.mw_built is None


def test_a_tranche_may_still_restate_what_the_claims_no_longer_do(session):
    """The reconciles run after the merge and fill an empty field from their own
    citations — that is re-citing a figure, not keeping an uncited one, and no
    clearing is recorded for it."""
    project = _row(session, mw_built=500.0)
    source = _cite(session, project, "https://example.test/a", mw_planned=600.0)
    source.blocks = json.dumps(
        [
            {
                "label": "Hall 1",
                "mw": 40.0,
                "status": "serving",
                "quotes": {"mw": "Hall 1 is serving customers with 40 MW."},
            }
        ]
    )
    session.flush()

    recompute_from_sources(session, project)
    assert [b.label for b in project.blocks] == ["Hall 1"]
    assert project.mw_built == 40.0
    assert "mw_built 500 -> empty" not in (project.notes or "")


def test_phase_with_nothing_behind_it_falls_back_to_the_default(session):
    project = _row(session, phase="operational")
    _cite(session, project, "https://example.test/a", mw_planned=100.0)

    recompute_from_sources(session, project)
    assert project.phase == DEFAULT_PHASE
    assert f"phase operational -> {DEFAULT_PHASE}" in project.notes


def test_identity_fields_are_never_cleared(session):
    """A name or a town is never overwritten once set, claims or no claims."""
    project = _row(session)
    _cite(session, project, "https://example.test/a", mw_planned=100.0)

    recompute_from_sources(session, project)
    assert (project.name, project.company, project.city) == (
        "Saline Township campus",
        "OpenAI",
        "Saline",
    )


def test_the_clearing_is_recorded_once_and_reads_back_as_settled(session):
    project = _row(session, first_announced=dt.date(2025, 10, 31))
    _cite(session, project, "https://example.test/a", mw_planned=100.0)

    recompute_from_sources(session, project)
    recompute_from_sources(session, project)

    assert project.notes.count("rule resolved `value_without_evidence`") == 1
    assert "value_without_evidence" in settled_codes(project)


def test_the_set_is_the_summed_facts_and_nothing_else():
    """Adding an identity or a derived field here would clear what is never
    re-stated by a claim — a name, a coordinate, the blocker."""
    assert {
        "customer",
        "mw_planned",
        "mw_built",
        "investment_usd",
        "phase",
        "first_announced",
        "expected_online",
    } == CLAIM_OWNED_FIELDS
