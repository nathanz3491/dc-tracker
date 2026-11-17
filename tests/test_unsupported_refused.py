"""A value no sentence states is never stored as a campus's own.

The first nightly spot check (2026-11-16) drew Amazon's Bridgefield, whose planned
capacity went from 121.62 MW to a 50 MW its only sentence did not state: a claim the
evidence gate could not confirm was the last resort, and the last resort was the
stored value. Now it stays on its citation, with its reason, and decides nothing.
"""

from __future__ import annotations

import json

from tracker.models import Project, Source
from tracker.upsert import (
    CLAIM_OWNED_FIELDS,
    DEFAULT_PHASE,
    claims_by_field,
    contenders,
    recompute_from_sources,
    unsupported,
)


def _row(session, **fields):
    project = Project(
        name="Bridgefield",
        company="Amazon",
        city="Ashburn",
        state="VA",
        dedup_key="amazon|city:ashburn|VA",
        confidence=1,
        **fields,
    )
    session.add(project)
    session.flush()
    return project


def _cite(session, project, url, claims, quotes=None, reasons=None):
    session.add(
        Source(
            project_id=project.id,
            url=url,
            source_type="general_media",
            claims=json.dumps(claims),
            quotes=json.dumps(quotes or {}),
            unconfirmed_fields=",".join(sorted(reasons)) if reasons else None,
            unconfirmed_reasons=json.dumps(reasons) if reasons else None,
        )
    )
    session.flush()
    session.refresh(project)


def test_a_value_its_sentence_does_not_state_empties_the_field(session):
    project = _row(session, mw_planned=121.62)
    _cite(
        session,
        project,
        "https://e.test/bridgefield",
        {"mw_planned": 50},
        {"mw_planned": "Amazon Data Services filed plans for a campus in Ashburn."},
        reasons={"mw_planned": "quote_off_target"},
    )
    recompute_from_sources(session, project)
    assert project.mw_planned is None
    assert "mw_planned" in (project.notes or "")
    # The claim is still on its citation, with its reason, for a reader to find.
    source = project.sources[0]
    assert json.loads(source.claims) == {"mw_planned": 50}
    assert json.loads(source.unconfirmed_reasons) == {"mw_planned": "quote_off_target"}


def test_every_kind_of_missing_sentence_is_refused(session):
    project = _row(session)
    _cite(
        session,
        project,
        "https://e.test/a",
        {"investment_usd": 1_000_000_000, "customer": "Meta", "first_announced": "2024-03-01"},
        reasons={
            "investment_usd": "no_quote",
            "customer": "quote_unverified",
            "first_announced": "quote_off_target",
        },
    )
    recompute_from_sources(session, project)
    assert project.investment_usd is None
    assert project.customer is None
    assert project.first_announced is None


def test_an_unstated_stage_falls_back_to_the_default(session):
    project = _row(session, phase="construction")
    _cite(
        session,
        project,
        "https://e.test/stage",
        {"phase": "construction"},
        {"phase": "The county approved the rezoning."},
        reasons={"phase": "quote_off_target"},
    )
    recompute_from_sources(session, project)
    assert project.phase == DEFAULT_PHASE


def test_a_quoted_value_still_decides(session):
    project = _row(session)
    _cite(
        session,
        project,
        "https://e.test/quoted",
        {"mw_planned": 300},
        {"mw_planned": "The 300 MW campus will be built in two phases."},
    )
    _cite(
        session,
        project,
        "https://e.test/unquoted",
        {"mw_planned": 500},
        reasons={"mw_planned": "no_quote"},
    )
    recompute_from_sources(session, project)
    assert project.mw_planned == 300


def test_the_refused_claim_is_out_of_every_reader_of_the_contest(session):
    """Ruled out in `claims_by_field`, so the conflict notes and the dispute finder,
    which read `contenders`, cannot offer it as a rival either."""
    project = _row(session)
    _cite(session, project, "https://e.test/u", {"mw_built": 40}, reasons={"mw_built": "no_quote"})
    [claim] = claims_by_field(list(project.sources))["mw_built"]
    assert claim.decided_against and not claim.confirmed
    assert contenders([claim]) == []


def test_only_a_campus_own_fields_are_refused():
    assert all(unsupported(name, confirmed=False) for name in CLAIM_OWNED_FIELDS)
    assert not unsupported("mw_planned", confirmed=True)
    assert not unsupported("county", confirmed=False)
    # The seed file's placeholders exist to fill a smoke-test row.
    assert not unsupported("mw_planned", confirmed=False, placeholder=True)
