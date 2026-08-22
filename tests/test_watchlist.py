"""The watchlist: what it stores, and what it decides a watch covers.

The matching rules are the point of this file. A watch that silently misses the
project somebody asked about is the one failure that makes the digest worthless,
so both directions of the loose name match and all three ways a company can be
attached to a project are asserted here.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tracker import watchlist
from tracker.models import Project, Watch


def _project(session, **kw) -> Project:
    """A minimally valid project row."""
    fields = {
        "name": "Colossus",
        "company": "xAI",
        "state": "TN",
        "city": "Memphis",
        "dedup_key": kw.get("dedup_key") or f"k{kw.get('name', 'Colossus')}{kw.get('company', '')}",
        **kw,
    }
    row = Project(**fields)
    session.add(row)
    session.flush()
    return row


# --- parsing ---------------------------------------------------------------


def test_parse_company_only():
    assert watchlist.parse("xAI") == ("xai", "")


def test_parse_company_and_project():
    assert watchlist.parse("Microsoft Corporation | Fairwater") == ("microsoft", "fairwater")


def test_a_bare_entry_is_a_company():
    """ "watch xAI" is the common case, so a lone token is the company part."""
    assert watchlist.parse("Colossus") == ("colossus", "")


def test_parse_refuses_a_separator_with_no_company():
    with pytest.raises(watchlist.WatchError):
        watchlist.parse(" | Fairwater")


# --- storing ---------------------------------------------------------------


def test_add_is_idempotent_across_spellings(session):
    """The UNIQUE constraint is on the normalized key, not on the text."""
    first, created = watchlist.add(session, "Microsoft Corporation")
    assert created
    second, created_again = watchlist.add(session, "Microsoft", note="the WI campuses")
    assert not created_again
    assert second.id == first.id
    assert second.note == "the WI campuses"
    # The text stays as whoever set the watch wrote it.
    assert second.entry == "Microsoft Corporation"
    assert len(watchlist.entries(session)) == 1


def test_a_company_and_one_of_its_projects_are_two_watches(session):
    watchlist.add(session, "xAI")
    watchlist.add(session, "xAI | Colossus")
    assert len(watchlist.entries(session)) == 2


def test_remove(session):
    watchlist.add(session, "xAI")
    assert watchlist.remove(session, "xai") is True
    assert watchlist.remove(session, "xAI") is False
    assert watchlist.entries(session) == []


def test_entries_are_in_the_order_they_were_added(session):
    watchlist.add(session, "xAI")
    watchlist.add(session, "Meta")
    watchlist.add(session, "Google")
    assert [w.entry for w in watchlist.entries(session)] == ["xAI", "Meta", "Google"]


# --- resolving -------------------------------------------------------------


def test_a_company_watch_covers_every_project_of_that_company(session):
    a = _project(session, name="Colossus", dedup_key="a")
    b = _project(session, name="Colossus 2", dedup_key="b")
    _project(session, name="Fairwater", company="Microsoft", state="WI", dedup_key="c")

    watchlist.add(session, "xAI")
    [entity] = watchlist.watched(session)
    assert set(entity.project_ids) == {a.id, b.id}
    assert entity.whole_company


def test_a_project_watch_narrows_to_that_project(session):
    a = _project(session, name="Colossus", dedup_key="a")
    _project(session, name="Some Other Site", dedup_key="b")

    watchlist.add(session, "xAI | Colossus")
    [entity] = watchlist.watched(session)
    assert entity.project_ids == (a.id,)


def test_the_name_match_is_loose_in_both_directions(session):
    """Typed short, stored long — and typed long, stored short."""
    stored_long = _project(session, name="Colossus 2 Memphis", dedup_key="a")
    stored_short = _project(session, name="Colossus", company="Nvidia", dedup_key="b")

    watchlist.add(session, "xAI | Colossus")
    watchlist.add(session, "Nvidia | Colossus phase two")
    first, second = watchlist.watched(session)
    assert first.project_ids == (stored_long.id,)
    assert second.project_ids == (stored_short.id,)


def test_a_watch_covers_projects_built_for_that_company(session):
    """The interesting news about a tenant is filed under the developer's name."""
    built_for = _project(
        session, name="Abilene", company="Crusoe", customer="OpenAI", dedup_key="a"
    )

    watchlist.add(session, "OpenAI")
    [entity] = watchlist.watched(session)
    assert entity.matches == {built_for.id: watchlist.VIA_CUSTOMER}


def test_the_operator_wins_when_a_company_is_both(session):
    """Meta building for Meta is reported once, as the builder."""
    both = _project(session, name="Hyperion", company="Meta", customer="Meta", dedup_key="a")

    watchlist.add(session, "Meta")
    [entity] = watchlist.watched(session)
    assert entity.matches == {both.id: watchlist.VIA_OPERATOR}


def test_a_watch_covers_a_block_leased_to_that_company(session):
    """Block-level customers exist because campus-level attribution is wrong."""
    from tracker.models import CapacityBlock

    campus = _project(session, name="Shared Campus", company="Aligned", dedup_key="a")
    session.add(
        CapacityBlock(
            project_id=campus.id, block_key="phase 2", label="Phase 2", customer="Anthropic"
        )
    )
    session.flush()

    watchlist.add(session, "Anthropic")
    [entity] = watchlist.watched(session)
    assert entity.matches == {campus.id: watchlist.VIA_BLOCK}


def test_an_unmatched_watch_resolves_to_nothing_rather_than_failing(session):
    """A watch set before the project exists is the normal case, not an error."""
    watchlist.add(session, "Nebius")
    [entity] = watchlist.watched(session)
    assert entity.project_ids == ()


def test_resolve_is_usable_without_a_database():
    """Structural, like `tracks.standing` — the docstring promises it."""

    class Row:
        def __init__(self, pid, company, name):
            self.id, self.company, self.name, self.customer = pid, company, name, None

    watch = Watch(entry="xAI", company_key="xai", project_key="", added_at=dt.datetime(2026, 1, 1))
    [entity] = watchlist.resolve([watch], [Row(7, "xAI", "Colossus"), Row(8, "Meta", "Hyperion")])
    assert entity.project_ids == (7,)
