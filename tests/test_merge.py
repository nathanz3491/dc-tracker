"""Folding rows that turned out to be one campus.

The interesting cases are all about what must NOT be lost: a citation, a
milestone's provenance, or the disagreement between two sources.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from tracker.merge import MergeError, merge_projects
from tracker.models import Event, Project, Risk, Source


def _project(session, name: str, company: str, **kwargs) -> Project:
    project = Project(
        name=name,
        company=company,
        state=kwargs.pop("state", "TX"),
        city=kwargs.pop("city", "Abilene"),
        dedup_key=f"{company.lower()}|city:abilene|TX",
        phase=kwargs.pop("phase", "construction"),
        confidence=kwargs.pop("confidence", 2),
        **kwargs,
    )
    session.add(project)
    session.flush()
    return project


def _source(session, project: Project, url: str, claims: str | None = None, **kwargs) -> Source:
    source = Source(
        project_id=project.id,
        url=url,
        source_type=kwargs.pop("source_type", "trade_press"),
        claims=claims,
        fields=kwargs.pop("fields", None),
        fetched_at=dt.datetime(2026, 1, 1),
    )
    session.add(source)
    session.flush()
    return source


def test_citations_move_and_the_duplicate_disappears(session):
    keep = _project(session, "Stargate Abilene", "Crusoe")
    dupe = _project(session, "Stargate", "Oracle")
    _source(session, keep, "https://a.test/1")
    _source(session, dupe, "https://b.test/2")

    result = merge_projects(session, keep.id, [dupe.id])

    assert result.sources_moved == 1
    assert session.get(Project, dupe.id) is None
    assert {s.url for s in session.get(Project, keep.id).sources} == {
        "https://a.test/1",
        "https://b.test/2",
    }


def test_a_moved_citation_is_not_swept_away_by_the_cascade(session):
    """`Project.sources` cascades delete-orphan.

    Setting `project_id` by hand leaves the row in the duplicate's collection, so
    deleting the duplicate takes the citation with it. Observed on the first live
    run, where it then failed on a foreign key. Reassignment has to go through the
    relationship.
    """
    keep = _project(session, "Stargate Abilene", "Crusoe")
    dupe = _project(session, "Stargate", "Oracle")
    _source(session, dupe, "https://only-copy.test/")

    merge_projects(session, keep.id, [dupe.id])

    assert session.scalar(select(func.count()).select_from(Source)) == 1, "the citation survived"


def test_the_same_citation_on_both_rows_is_not_duplicated(session):
    keep = _project(session, "Stargate Abilene", "Crusoe")
    dupe = _project(session, "Stargate", "Oracle")
    _source(session, keep, "https://shared.test/")
    _source(session, dupe, "https://shared.test/")

    result = merge_projects(session, keep.id, [dupe.id])

    assert result.sources_discarded == 1
    assert session.scalar(select(func.count()).select_from(Source)) == 1


def test_a_milestone_keeps_its_provenance_when_its_citation_is_collapsed(session):
    """The duplicate's copy of a shared citation is deleted, so anything pointing
    at it must be repointed first — otherwise ON DELETE SET NULL quietly strips
    the evidence from a milestone."""
    keep = _project(session, "Stargate Abilene", "Crusoe")
    dupe = _project(session, "Stargate", "Oracle")
    _source(session, keep, "https://shared.test/")
    dupe_source = _source(session, dupe, "https://shared.test/")
    session.add(
        Event(
            project_id=dupe.id,
            event_date=dt.date(2026, 3, 1),
            event_type="groundbreaking",
            description="broke ground",
            source_id=dupe_source.id,
        )
    )
    session.flush()

    merge_projects(session, keep.id, [dupe.id])

    event = session.scalar(select(Event))
    assert event.project_id == keep.id
    assert event.source_id is not None, "provenance survived the collapse"


def test_duplicate_milestones_collapse_rather_than_trip_the_constraint(session):
    keep = _project(session, "Stargate Abilene", "Crusoe")
    dupe = _project(session, "Stargate", "Oracle")
    for project in (keep, dupe):
        session.add(
            Event(
                project_id=project.id,
                event_date=dt.date(2026, 3, 1),
                event_type="groundbreaking",
                description="broke ground",
            )
        )
    session.flush()

    result = merge_projects(session, keep.id, [dupe.id])

    assert result.events_discarded == 1
    assert session.scalar(select(func.count()).select_from(Event)) == 1


def test_obstacles_move_and_collapse_on_their_key(session):
    keep = _project(session, "Stargate Abilene", "Crusoe")
    dupe = _project(session, "Stargate", "Oracle")
    session.add(Risk(project_id=keep.id, category="water", severity="watch", summary="a"))
    session.add(Risk(project_id=dupe.id, category="water", severity="material", summary="b"))
    session.add(Risk(project_id=dupe.id, category="permitting", severity="watch", summary="c"))
    session.flush()

    result = merge_projects(session, keep.id, [dupe.id])

    assert (result.risks_moved, result.risks_discarded) == (1, 1)
    assert {r.category for r in session.get(Project, keep.id).risks} == {"water", "permitting"}


def test_fields_are_recomputed_from_the_combined_citations(session):
    """The surviving row's values come from the merged evidence, not from itself.

    Which id an operator keeps must not decide the data — otherwise the merge is a
    judgement about capacity rather than about identity.
    """
    keep = _project(session, "Stargate Abilene", "Crusoe", mw_planned=None)
    dupe = _project(session, "Stargate", "Oracle", mw_planned=1200)
    _source(
        session,
        dupe,
        "https://filing.test/",
        claims='{"mw_planned": 1200}',
        fields="mw_planned",
        source_type="company_filing",
    )

    merge_projects(session, keep.id, [dupe.id])

    assert session.get(Project, keep.id).mw_planned == 1200


def test_merging_recovers_corroboration(session):
    """Four single-source rows become one row with four independent domains.

    A pleasant consequence rather than the goal: the duplicates were each capped
    below full confidence for having only one citation, and they were only ever
    one project.
    """
    keep = _project(session, "Stargate Abilene", "Crusoe")
    others = [_project(session, "Stargate", c) for c in ("Oracle", "OpenAI", "SoftBank")]
    claims = '{"mw_planned": 1200, "phase": "construction"}'
    for i, project in enumerate([keep, *others]):
        _source(
            session,
            project,
            f"https://outlet{i}.test/story",
            claims=claims,
            fields="mw_planned,phase",
        )

    merge_projects(session, keep.id, [p.id for p in others])

    survivor = session.get(Project, keep.id)
    assert len(survivor.sources) == 4
    assert survivor.confidence == 3


def test_the_merge_is_recorded_where_it_cannot_be_regenerated_away(session):
    """A later upsert rebuilds every `[tracker]` line wholesale.

    A merge is a one-off operator decision, so it is written as plain prose — the
    one class of note the notes rebuilder never touches.
    """
    keep = _project(session, "Stargate Abilene", "Crusoe")
    dupe = _project(session, "Stargate", "Oracle")

    merge_projects(session, keep.id, [dupe.id])

    notes = session.get(Project, keep.id).notes or ""
    assert f"#{dupe.id}" in notes
    assert not notes.strip().startswith("[")


def test_refuses_a_merge_it_cannot_make_sense_of(session):
    keep = _project(session, "Stargate Abilene", "Crusoe")
    with pytest.raises(MergeError, match="does not exist"):
        merge_projects(session, 9999, [keep.id])
    with pytest.raises(MergeError, match="nothing to merge"):
        merge_projects(session, keep.id, [keep.id])
    with pytest.raises(MergeError, match="does not exist"):
        merge_projects(session, keep.id, [9999])
