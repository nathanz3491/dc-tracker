"""The digest: what counts as news, which way it cuts, and what leads.

The load-bearing assertion in this file is `test_the_window_filters_on_when_we
_learned_it`. Every other date in the schema answers a different question, and a
digest keyed on the wrong one either repeats 2022 every morning or hides what
arrived last night.
"""

from __future__ import annotations

import datetime as dt

from tracker import feed, watchlist
from tracker.models import Event, Project, Risk, Source
from tracker.vocab import EVENT_TYPES

NOW = dt.datetime(2026, 8, 22, 9, 0)
SINCE = dt.datetime(2026, 8, 20, 0, 0)
BEFORE = dt.datetime(2026, 8, 1, 12, 0)


def _project(session, **kw) -> Project:
    row = Project(
        **{
            "name": "Colossus",
            "company": "xAI",
            "state": "TN",
            "city": "Memphis",
            "dedup_key": kw.pop("dedup_key", "xai|city:memphis|TN"),
            "created_at": BEFORE,
            "updated_at": BEFORE,
            **kw,
        }
    )
    session.add(row)
    session.flush()
    return row


def _source(session, project, url="https://trade.example/story", when=NOW) -> Source:
    row = Source(
        project_id=project.id,
        url=url,
        source_type="trade_press",
        fetched_at=when,
        published_at=when,
    )
    session.add(row)
    session.flush()
    return row


def _event(session, project, **kw) -> Event:
    row = Event(
        **{
            "project_id": project.id,
            "event_date": dt.date(2026, 8, 21),
            "event_type": "energized",
            "description": "Site energized.",
            "quote": "The site was energized on Friday.",
            "created_at": NOW,
            **kw,
        }
    )
    session.add(row)
    session.flush()
    return row


def _risk(session, project, **kw) -> Risk:
    row = Risk(
        **{
            "project_id": project.id,
            "category": "community_opposition",
            "severity": "material",
            "status": "open",
            "summary": "Neighbours object to turbine noise.",
            "quote": "Residents told the board the turbines are audible at night.",
            "created_at": NOW,
            **kw,
        }
    )
    session.add(row)
    session.flush()
    return row


# --- the vocabulary is complete -------------------------------------------


def test_every_event_type_has_a_sign_and_a_scale():
    """A new milestone type must not fall through to "neutral, weight 1" silently."""
    assert set(feed.EVENT_SIGN) == set(EVENT_TYPES)
    assert set(feed.SCALE) == set(EVENT_TYPES)


def test_every_milestone_belongs_to_a_track():
    """Inverted from `tracks.TRACK_MILESTONES`, so the two cannot disagree."""
    assert set(feed.EVENT_TRACK) == set(EVENT_TYPES) - {"delayed", "expanded"}


# --- the two clocks --------------------------------------------------------


def test_the_window_filters_on_when_we_learned_it(session):
    """A 2022 milestone read last night is news; today's milestone read in July is not."""
    project = _project(session)
    _event(
        session,
        project,
        event_type="land_acquired",
        event_date=dt.date(2022, 3, 4),
        description="Bought the land.",
        created_at=NOW,
    )
    _event(
        session,
        project,
        event_type="groundbreaking",
        event_date=dt.date(2026, 8, 21),
        description="Broke ground.",
        created_at=BEFORE,
    )

    result = feed.digest(session, since=SINCE)
    assert [s.label for s in result.signals] == ["land_acquired"]
    # And it reports both clocks, so the old date is visible rather than dressed up.
    assert result.signals[0].happened == dt.date(2022, 3, 4)
    assert result.signals[0].at == NOW


def test_an_event_with_no_discovery_date_is_not_news(session):
    """NULL means "we do not know when we learned this" (migration 0018)."""
    project = _project(session)
    _event(session, project, created_at=None)
    assert feed.digest(session, since=SINCE).signals == ()


# --- which way it cuts -----------------------------------------------------


def test_a_milestone_is_good_and_an_obstacle_is_bad(session):
    project = _project(session)
    _event(session, project, event_type="first_customer", description="Anchor tenant signed.")
    _risk(session, project)

    result = feed.digest(session, since=SINCE)
    assert {(s.kind, s.sign) for s in result.signals} == {
        ("milestone", "good"),
        ("obstacle_opened", "bad"),
    }


def test_an_announcement_is_neither_good_nor_bad(session):
    project = _project(session)
    _event(session, project, event_type="announced", description="Campus announced.")
    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.sign == "neutral"


def test_a_cleared_obstacle_is_good_news_dated_by_its_resolution(session):
    """Nothing records when we *read* that a risk cleared, so `resolved_at` is the clock."""
    project = _project(session)
    _risk(
        session,
        project,
        status="resolved",
        resolved_at=dt.date(2026, 8, 21),
        # Learned long before the window: it is the resolution that is new.
        created_at=BEFORE,
    )
    [signal] = feed.digest(session, since=SINCE).signals
    assert (signal.kind, signal.sign) == ("obstacle_cleared", "good")
    assert signal.happened == dt.date(2026, 8, 21)


def test_a_resolved_obstacle_outside_the_window_is_silent(session):
    project = _project(session)
    _risk(session, project, status="resolved", resolved_at=dt.date(2026, 7, 1), created_at=BEFORE)
    assert feed.digest(session, since=SINCE).signals == ()


def test_a_new_project_is_its_own_signal(session):
    _project(session, created_at=NOW, mw_planned=250.0)
    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.kind == "new_project"
    assert "250 MW" in signal.detail


# --- materiality -----------------------------------------------------------


def test_the_milestone_a_blocked_track_was_waiting_for_scores_highest(session):
    """Power blocked, then an interconnection agreement: the whole point of the page."""
    project = _project(session)
    _risk(session, project, category="grid_capacity", severity="blocking", created_at=BEFORE)
    _event(
        session,
        project,
        event_type="interconnection_agreement",
        event_date=dt.date(2026, 8, 21),
        description="Interconnection agreement signed with the utility.",
    )

    [signal, *_] = feed.digest(session, since=SINCE).signals
    assert signal.label == "interconnection_agreement"
    assert signal.unblocks
    assert signal.weight == feed.SCALE["interconnection_agreement"] + feed.UNBLOCKS_BONUS
    assert "was the blocker" in signal.effect


def test_an_advance_on_an_unblocked_track_says_so_plainly(session):
    project = _project(session)
    _event(session, project, event_type="site_work", description="Grading started.")
    [signal] = feed.digest(session, since=SINCE).signals
    assert not signal.unblocks
    assert signal.effect == "construction advanced to site work"
    assert signal.track == "construction"


def test_bad_news_outranks_good_news_of_the_same_weight():
    good = feed.Signal("milestone", "good", 1, "xAI", "A", "energized", "d", weight=3)
    bad = feed.Signal("milestone", "bad", 2, "xAI", "B", "delayed", "d", weight=3)
    assert [s.sign for s in feed.rank([good, bad])] == ["bad", "good"]


def test_an_unclassified_obstacle_is_not_placed_on_a_track(session):
    project = _project(session)
    _risk(session, project, category="unclassified")
    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.track is None and signal.effect is None


# --- evidence --------------------------------------------------------------


def test_an_unconfirmed_signal_is_held_out_of_the_headline(session):
    project = _project(session)
    _event(session, project, quote=None, unconfirmed="no_quote")
    result = feed.digest(session, since=SINCE)
    assert result.signals == ()
    assert [s.label for s in result.held] == ["energized"]


def test_a_signal_carries_its_publisher(session):
    project = _project(session)
    source = _source(session, project, url="https://www.datacenterdynamics.com/x")
    _event(session, project, source_id=source.id)
    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.publisher == "datacenterdynamics.com"
    assert signal.source_url == "https://www.datacenterdynamics.com/x"


def test_the_last_crawl_is_reported_so_a_dead_crawler_is_visible(session):
    project = _project(session)
    _source(session, project, when=dt.datetime(2026, 8, 19, 3, 0))
    assert feed.digest(session, since=SINCE).last_crawl == dt.datetime(2026, 8, 19, 3, 0)


# --- scope -----------------------------------------------------------------


def test_with_no_watchlist_the_whole_database_is_read(session):
    _project(session, created_at=NOW)
    result = feed.digest(session, since=SINCE)
    assert result.watching_everything
    assert len(result.signals) == 1


def test_a_watchlist_scopes_the_digest(session):
    watched = _project(session, created_at=NOW)
    _project(session, company="Meta", name="Hyperion", state="LA", dedup_key="m", created_at=NOW)

    watchlist.add(session, "xAI")
    result = feed.digest(session, since=SINCE)
    assert not result.watching_everything
    assert [s.project_id for s in result.signals] == [watched.id]
    assert result.projects_watched == 1


def test_each_watched_entity_gets_its_own_tally(session):
    xai = _project(session)
    meta = _project(session, company="Meta", name="Hyperion", state="LA", dedup_key="m")
    _event(session, xai, event_type="energized", description="Energized.")
    _risk(session, meta)
    _event(
        session,
        meta,
        event_type="delayed",
        event_date=dt.date(2026, 8, 21),
        description="Slipped a year.",
        quote=None,
        unconfirmed="no_quote",
    )

    watchlist.add(session, "xAI")
    watchlist.add(session, "Meta")
    result = feed.digest(session, since=SINCE)

    by_entry = {e.entry: e for e in result.entities}
    assert (by_entry["xAI"].good, by_entry["xAI"].bad) == (1, 0)
    assert (by_entry["Meta"].good, by_entry["Meta"].bad) == (0, 1)
    # The unconfirmed slip is counted as held rather than as news.
    assert by_entry["Meta"].held == 1


def test_a_signal_names_the_watch_that_brought_it_in(session):
    project = _project(session, company="Crusoe", customer="OpenAI", name="Abilene", dedup_key="c")
    _event(session, project, description="Energized.")

    watchlist.add(session, "OpenAI")
    [signal] = feed.digest(session, since=SINCE).signals
    assert (signal.entry, signal.via) == ("OpenAI", watchlist.VIA_CUSTOMER)


def test_the_default_window_is_a_week(session):
    """`days` and an explicit `since` are the same knob."""
    project = _project(session)
    _event(session, project, created_at=dt.datetime.now() - dt.timedelta(days=2))
    assert len(feed.digest(session).signals) == 1
    assert feed.digest(session, days=1).signals == ()


def test_as_json_is_serializable(session):
    import json

    project = _project(session)
    _event(session, project)
    _risk(session, project)
    payload = feed.digest(session, since=SINCE).as_json()
    assert json.loads(json.dumps(payload))["counts"]["total"] == 2


# --- what the real database found -----------------------------------------


def test_a_future_dated_milestone_is_a_schedule_not_an_achievement(session):
    """Hyperion's "full Phase 1 expected online 2028", read as good news, was wrong."""
    project = _project(session)
    _risk(session, project, category="grid_capacity", severity="blocking", created_at=BEFORE)
    _event(
        session,
        project,
        event_type="energized",
        event_date=dt.date(2028, 1, 1),
        description="Full Phase 1 expected online.",
    )

    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.expected
    assert signal.sign == "neutral"
    assert not signal.unblocks
    assert signal.weight == 1
    assert "not there yet" in signal.effect


def test_a_milestone_dated_today_has_happened(session):
    """The boundary: `as_of` is inclusive, as it is in `tracks.standing`."""
    project = _project(session)
    _event(session, project, event_date=dt.date.today(), description="Energized today.")
    [signal] = feed.digest(session, since=SINCE).signals
    assert not signal.expected and signal.sign == "good"


def test_two_articles_reporting_one_moment_fold_into_one_signal(session):
    """The same withdrawal, twice, with two dates — seen live on Louisa County."""
    project = _project(session)
    _event(
        session,
        project,
        event_type="delayed",
        event_date=dt.date(2025, 6, 1),
        description="AWS withdrew CUP application amid neighbour opposition.",
    )
    _event(
        session,
        project,
        event_type="delayed",
        event_date=dt.date(2025, 7, 1),
        description="AWS withdraws CUP application.",
    )

    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.restatements == 1
    # The database still holds both rows; only the digest folds them.
    assert len(project.events) == 2


def test_folding_does_not_hide_a_quoted_signal_behind_an_unquoted_one(session):
    project = _project(session)
    _event(session, project, event_date=dt.date(2026, 8, 21), description="Energized.")
    _event(
        session,
        project,
        event_date=dt.date(2026, 8, 20),
        description="Energized, so somebody said.",
        quote=None,
        unconfirmed="no_quote",
    )

    result = feed.digest(session, since=SINCE)
    assert [s.detail for s in result.signals] == ["Energized."]
    assert [s.detail for s in result.held] == ["Energized, so somebody said."]


def test_folding_keeps_different_milestones_apart(session):
    project = _project(session)
    _event(session, project, event_type="energized", description="Energized.")
    _event(session, project, event_type="first_customer", description="Tenant signed.")
    assert len(feed.digest(session, since=SINCE).signals) == 2


# --- when to interrupt somebody -------------------------------------------


def test_the_blocker_moving_notifies(session):
    project = _project(session)
    _risk(session, project, category="grid_capacity", severity="blocking", created_at=BEFORE)
    _event(
        session,
        project,
        event_type="interconnection_agreement",
        event_date=dt.date(2026, 8, 21),
        description="Agreement signed.",
    )
    [signal] = [s for s in feed.digest(session, since=SINCE).signals if s.label != "grid_capacity"]
    assert signal.notify


def test_a_decisive_milestone_notifies_and_a_cheap_one_does_not(session):
    """The five things worth a notification, and the ones that are page-only."""
    project = _project(session)
    for kind, date in (
        ("energized", dt.date(2026, 8, 21)),
        ("first_customer", dt.date(2026, 8, 20)),
        ("delayed", dt.date(2026, 8, 19)),
        ("announced", dt.date(2026, 8, 18)),
        ("permit_filed", dt.date(2026, 8, 17)),
        ("land_acquired", dt.date(2026, 8, 16)),
        ("site_work", dt.date(2026, 8, 15)),
    ):
        _event(session, project, event_type=kind, event_date=date, description=f"{kind}.")

    by_label = {s.label: s for s in feed.digest(session, since=SINCE).signals}
    assert [k for k in by_label if by_label[k].notify] != []
    assert all(by_label[k].notify for k in ("energized", "first_customer", "delayed"))
    assert not any(
        by_label[k].notify for k in ("announced", "permit_filed", "land_acquired", "site_work")
    )


def test_a_material_obstacle_notifies_and_a_watch_one_does_not(session):
    """The case this was asked for: a local group objecting is recorded `material`."""
    loud = _project(session)
    quiet = _project(session, name="Quiet", dedup_key="q")
    _risk(session, loud, severity="material")
    _risk(session, quiet, severity="watch")

    by_project = {s.project_id: s for s in feed.digest(session, since=SINCE).signals}
    assert by_project[loud.id].notify
    assert not by_project[quiet.id].notify


def test_a_cleared_obstacle_notifies_only_if_it_mattered(session):
    project = _project(session)
    _risk(
        session,
        project,
        severity="material",
        status="resolved",
        resolved_at=dt.date(2026, 8, 21),
        created_at=BEFORE,
    )
    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.kind == "obstacle_cleared" and signal.notify


def test_an_unconfirmed_signal_never_notifies(session):
    """A sentence no quote stood up for does not get to interrupt anybody."""
    project = _project(session)
    _event(session, project, quote=None, unconfirmed="no_quote")
    result = feed.digest(session, since=SINCE)
    assert result.notifying == ()
    assert not result.held[0].notify


def test_a_scheduled_milestone_never_notifies(session):
    project = _project(session)
    _event(
        session,
        project,
        event_type="energized",
        event_date=dt.date(2028, 1, 1),
        description="Expected online.",
    )
    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.expected and not signal.notify


def test_a_new_project_is_page_only(session):
    """Worth knowing, not worth an interruption."""
    _project(session, created_at=NOW)
    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.kind == "new_project" and not signal.notify


def test_the_notifying_subset_is_counted_and_ranked(session):
    project = _project(session)
    _event(session, project, event_type="energized", description="Energized.")
    _event(
        session,
        project,
        event_type="announced",
        event_date=dt.date(2026, 8, 20),
        description="Announced.",
    )
    result = feed.digest(session, since=SINCE)
    assert [s.label for s in result.notifying] == ["energized"]
    assert result.as_json()["counts"]["notify"] == 1


def test_a_cleared_obstacle_says_cleared_in_its_own_title(session):
    """The category alone made a resolved risk read as a live one on real data."""
    project = _project(session)
    _risk(
        session,
        project,
        status="resolved",
        resolved_at=dt.date(2026, 8, 21),
        created_at=BEFORE,
        summary="Operating turbines without an air permit.",
    )
    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.headline == "community opposition — cleared"


def test_an_open_obstacle_and_a_milestone_are_titled_apart(session):
    project = _project(session)
    _risk(session, project, category="water")
    _event(session, project, event_type="energized", description="Energized.")
    titles = {s.headline for s in feed.digest(session, since=SINCE).signals}
    assert titles == {"water — obstacle", "energized"}


def test_every_kind_a_digest_produces_is_declared(session):
    """`KINDS` is the list a reader is given; a fifth kind must not appear silently."""
    project = _project(session, created_at=NOW)
    _event(session, project, description="Energized.")
    _risk(session, project)
    _risk(
        session,
        project,
        category="water",
        status="resolved",
        resolved_at=dt.date(2026, 8, 21),
        created_at=BEFORE,
    )
    produced = {s.kind for s in feed.digest(session, since=SINCE).signals}
    assert produced == set(feed.KINDS)


def test_a_chips_tally_matches_the_cards_that_chip_filters_to(session):
    """The number above the list and the list have to agree.

    They did not. Tallies were counted from the unfolded signals while the card
    list was folded, so one moment reported by three publishers was three updates
    in the chip and one card underneath it — measured live at 134 against 41.
    """
    project = _project(session)
    # One moment, three publishers: same project, same kind, same label.
    for i in range(3):
        citation = _source(session, project, url=f"https://trade.example/{i}")
        _event(session, project, source_id=citation.id, event_date=dt.date(2026, 8, 18 + i))
    watchlist.add(session, "xAI")

    got = feed.digest(session, since=SINCE)

    tally = {e.entry: e for e in got.entities}["xAI"]
    cards = [s for s in got.signals if s.entry == "xAI"]
    assert len(cards) == 1, "three articles, one moment"
    assert cards[0].restatements == 2
    assert tally.total == len(cards), "the chip must count what clicking it shows"
    assert tally.good == 1


def test_the_page_limit_does_not_shrink_the_tally(session):
    """The chip describes the window; the limit describes the page."""
    project = _project(session)
    for i, kind in enumerate(("energized", "land_acquired", "permit_approved")):
        citation = _source(session, project, url=f"https://trade.example/{i}")
        _event(session, project, event_type=kind, source_id=citation.id)
    watchlist.add(session, "xAI")

    got = feed.digest(session, since=SINCE, limit=1)

    assert len(got.signals) == 1
    assert {e.entry: e for e in got.entities}["xAI"].total == 3
