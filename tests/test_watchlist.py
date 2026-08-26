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


def test_add_is_idempotent_across_spellings(session, account):
    """The UNIQUE constraint is on the normalized key, not on the text."""
    first, created = watchlist.add(session, "Microsoft Corporation", account_id=account.id)
    assert created
    second, created_again = watchlist.add(
        session, "Microsoft", account_id=account.id, note="the WI campuses"
    )
    assert not created_again
    assert second.id == first.id
    assert second.note == "the WI campuses"
    # The text stays as whoever set the watch wrote it.
    assert second.entry == "Microsoft Corporation"
    assert len(watchlist.entries(session)) == 1


def test_a_company_and_one_of_its_projects_are_two_watches(session, account):
    watchlist.add(session, "xAI", account_id=account.id)
    watchlist.add(session, "xAI | Colossus", account_id=account.id)
    assert len(watchlist.entries(session)) == 2


def test_remove(session, account):
    watchlist.add(session, "xAI", account_id=account.id)
    assert watchlist.remove(session, "xai", account_id=account.id) is True
    assert watchlist.remove(session, "xAI", account_id=account.id) is False
    assert watchlist.entries(session) == []


def test_entries_are_in_the_order_they_were_added(session, account):
    watchlist.add(session, "xAI", account_id=account.id)
    watchlist.add(session, "Meta", account_id=account.id)
    watchlist.add(session, "Google", account_id=account.id)
    assert [w.entry for w in watchlist.entries(session)] == ["xAI", "Meta", "Google"]


# --- resolving -------------------------------------------------------------


def test_a_company_watch_covers_every_project_of_that_company(session, account):
    a = _project(session, name="Colossus", dedup_key="a")
    b = _project(session, name="Colossus 2", dedup_key="b")
    _project(session, name="Fairwater", company="Microsoft", state="WI", dedup_key="c")

    watchlist.add(session, "xAI", account_id=account.id)
    [entity] = watchlist.watched(session)
    assert set(entity.project_ids) == {a.id, b.id}
    assert entity.whole_company


def test_a_project_watch_narrows_to_that_project(session, account):
    a = _project(session, name="Colossus", dedup_key="a")
    _project(session, name="Some Other Site", dedup_key="b")

    watchlist.add(session, "xAI | Colossus", account_id=account.id)
    [entity] = watchlist.watched(session)
    assert entity.project_ids == (a.id,)


def test_the_name_match_is_loose_in_both_directions(session, account):
    """Typed short, stored long — and typed long, stored short."""
    stored_long = _project(session, name="Colossus 2 Memphis", dedup_key="a")
    stored_short = _project(session, name="Colossus", company="Nvidia", dedup_key="b")

    watchlist.add(session, "xAI | Colossus", account_id=account.id)
    watchlist.add(session, "Nvidia | Colossus phase two", account_id=account.id)
    first, second = watchlist.watched(session)
    assert first.project_ids == (stored_long.id,)
    assert second.project_ids == (stored_short.id,)


def test_a_watch_covers_projects_built_for_that_company(session, account):
    """The interesting news about a tenant is filed under the developer's name."""
    built_for = _project(
        session, name="Abilene", company="Crusoe", customer="OpenAI", dedup_key="a"
    )

    watchlist.add(session, "OpenAI", account_id=account.id)
    [entity] = watchlist.watched(session)
    assert entity.matches == {built_for.id: watchlist.VIA_CUSTOMER}


def test_the_operator_wins_when_a_company_is_both(session, account):
    """Meta building for Meta is reported once, as the builder."""
    both = _project(session, name="Hyperion", company="Meta", customer="Meta", dedup_key="a")

    watchlist.add(session, "Meta", account_id=account.id)
    [entity] = watchlist.watched(session)
    assert entity.matches == {both.id: watchlist.VIA_OPERATOR}


def test_a_watch_covers_a_block_leased_to_that_company(session, account):
    """Block-level customers exist because campus-level attribution is wrong."""
    from tracker.models import CapacityBlock

    campus = _project(session, name="Shared Campus", company="Aligned", dedup_key="a")
    session.add(
        CapacityBlock(
            project_id=campus.id, block_key="phase 2", label="Phase 2", customer="Anthropic"
        )
    )
    session.flush()

    watchlist.add(session, "Anthropic", account_id=account.id)
    [entity] = watchlist.watched(session)
    assert entity.matches == {campus.id: watchlist.VIA_BLOCK}


def test_an_unmatched_watch_resolves_to_nothing_rather_than_failing(session, account):
    """A watch set before the project exists is the normal case, not an error."""
    watchlist.add(session, "Nebius", account_id=account.id)
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


# --- ownership -------------------------------------------------------------
#
# A watchlist belongs to an account (migration 0021). These are the properties
# that made that worth a migration: two readers do not collide, do not see each
# other, and cannot delete each other's entries.


@pytest.fixture
def two_accounts(session):
    from tracker import accounts

    return (
        accounts.create(session, "alice@example.com", "correct horse"),
        accounts.create(session, "bob@example.com", "correct horse"),
    )


def test_two_accounts_can_watch_the_same_company(session, two_accounts):
    """The UNIQUE constraint is per account, or the second person is refused."""
    alice, bob = two_accounts
    _, first = watchlist.add(session, "xAI", account_id=alice.id)
    _, second = watchlist.add(session, "xAI", account_id=bob.id)
    assert first and second, "both are new rows"
    assert len(watchlist.entries(session)) == 2


def test_a_reader_sees_only_their_own(session, two_accounts):
    alice, bob = two_accounts
    watchlist.add(session, "xAI", account_id=alice.id)
    watchlist.add(session, "Meta", account_id=bob.id)

    assert [w.entry for w in watchlist.entries(session, account_id=alice.id)] == ["xAI"]
    assert [w.entry for w in watchlist.entries(session, account_id=bob.id)] == ["Meta"]


def test_no_account_means_every_account(session, two_accounts):
    """What the CLI reads: a terminal sees the database, not one person's slice."""
    alice, bob = two_accounts
    watchlist.add(session, "xAI", account_id=alice.id)
    watchlist.add(session, "Meta", account_id=bob.id)
    assert {w.entry for w in watchlist.entries(session)} == {"xAI", "Meta"}


def test_one_reader_cannot_remove_another_s_entry(session, two_accounts):
    """On a shared list this was not even expressible. It is the second reason 0021 exists."""
    alice, bob = two_accounts
    watchlist.add(session, "xAI", account_id=alice.id)

    assert watchlist.remove(session, "xAI", account_id=bob.id) is False
    assert len(watchlist.entries(session, account_id=alice.id)) == 1, "hers survived"


def test_adding_twice_updates_one_row_rather_than_colliding(session, account):
    """Idempotent per account, on the normalized key rather than on the text."""
    watchlist.add(session, "Microsoft Corporation", account_id=account.id, note="first")
    row, created = watchlist.add(session, "microsoft", account_id=account.id, note="second")
    assert created is False
    assert row.note == "second"
    assert row.entry == "Microsoft Corporation", "their spelling is not rewritten"
    assert len(watchlist.entries(session, account_id=account.id)) == 1


def test_the_whole_database_view_carries_an_owner(session, two_accounts):
    """The CLI prints it as a column; without it the rows run together."""
    alice, bob = two_accounts
    watchlist.add(session, "xAI", account_id=alice.id)
    watchlist.add(session, "Meta", account_id=bob.id)

    owners = {e.entry: e.owner for e in watchlist.watched(session)}
    assert owners == {"xAI": "alice@example.com", "Meta": "bob@example.com"}


def test_one_account_s_view_does_not_carry_an_owner(session, account):
    """It would be telling the page something it already knows, and it is a payload
    that *could* carry another account's address."""
    watchlist.add(session, "xAI", account_id=account.id)
    [entity] = watchlist.watched(session, account_id=account.id)
    assert entity.owner is None
    assert "owner" not in entity.as_json()
