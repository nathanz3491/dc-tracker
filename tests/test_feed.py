"""The digest: what counts as news, which way it cuts, and what leads.

The load-bearing assertion in this file is `test_the_window_filters_on_when_it_was
_reported`. Every other date in the schema answers a different question, and a
digest keyed on the wrong one either repeats 2015 every morning because the crawler
found it yesterday, or hides what was reported last week.
"""

from __future__ import annotations

import datetime as dt

from tracker import feed, watchlist
from tracker.models import Event, Project, Risk, Source
from tracker.vocab import EVENT_TYPES

#: Relative to the real today, because the window is: nothing reported more than
#: `feed.REPORT_WINDOW_DAYS` ago is an update, and a suite pinned to fixed dates
#: silently stops testing anything the day those dates age out of it.
TODAY = dt.date.today()
NOW = dt.datetime.combine(TODAY, dt.time(9, 0))
SINCE = dt.datetime.combine(TODAY - dt.timedelta(days=2), dt.time.min)
BEFORE = dt.datetime.combine(TODAY - dt.timedelta(days=21), dt.time(12, 0))
LONG_AGO = dt.datetime.combine(TODAY - dt.timedelta(days=400), dt.time(12, 0))


def day(offset: int) -> dt.date:
    """`TODAY` plus `offset` days."""
    return TODAY + dt.timedelta(days=offset)


def _project(session, **kw) -> Project:
    row = Project(
        **{
            "name": "Colossus",
            "company": "xAI",
            "state": "TN",
            "city": "Memphis",
            "dedup_key": kw.pop("dedup_key", "xai|city:memphis|TN"),
            # Long enough ago that the row's own arrival is outside every window,
            # so a test about milestones is not also about "new to the tracker".
            "created_at": LONG_AGO,
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
            "event_date": day(-1),
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


def test_the_window_filters_on_when_it_was_reported(session):
    """A 2022 milestone read last night is not news; one reported this week is."""
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
        event_date=day(-1),
        description="Broke ground.",
        created_at=NOW - dt.timedelta(hours=2),
    )

    result = feed.digest(session, since=SINCE)
    assert [s.label for s in result.signals] == ["groundbreaking"]
    assert result.signals[0].reported == day(-1)


def test_an_article_from_2015_found_yesterday_is_not_an_update(session):
    """The case that started this: the crawler reads an old article today. Its
    publish date is the report date, and it is years outside the window."""
    project = _project(session)
    old = _source(session, project, when=dt.datetime(2015, 5, 1))
    _event(
        session,
        project,
        source_id=old.id,
        event_type="energized",
        event_date=dt.date(2015, 4, 20),
        created_at=NOW,
    )
    assert [s for s in feed.digest(session, days=60).signals if s.kind == "milestone"] == []


def test_the_article_date_outranks_the_milestone_date(session):
    """A late first report — an energisation in spring, first written up last week —
    is reported last week, and lands in last week's window."""
    project = _project(session)
    fresh = _source(session, project, when=NOW - dt.timedelta(days=5))
    _event(session, project, source_id=fresh.id, event_date=day(-90), created_at=NOW)

    [signal] = [s for s in feed.digest(session, days=7).signals if s.kind == "milestone"]
    assert signal.reported == day(-5)
    assert signal.happened == day(-90)


def test_a_recent_article_recalling_an_old_milestone_is_background(session):
    """ "The campus, which broke ground in 2023, ..." — reported this week, and still
    not an update: the milestone predates its own article by more than a year."""
    project = _project(session)
    recap = _source(session, project, when=NOW - dt.timedelta(days=2))
    _event(
        session,
        project,
        source_id=recap.id,
        event_type="groundbreaking",
        event_date=day(-2 - feed.BACKGROUND_DAYS - 1),
        created_at=NOW,
    )
    assert [s for s in feed.digest(session, days=7).signals if s.kind == "milestone"] == []
    assert feed.background(NOW - dt.timedelta(days=2), day(-2 - feed.BACKGROUND_DAYS - 1))
    assert not feed.background(NOW - dt.timedelta(days=2), day(-2 - feed.BACKGROUND_DAYS))


def test_a_fact_cannot_have_been_reported_after_we_stored_it():
    """A later citation is a re-report, not the first: Fairwater's energisation,
    stored 08-11 and re-pointed at a 10-02 article, read as October news."""
    stored = NOW - dt.timedelta(days=50)
    assert feed.reported_on(day(-4), day(-120), stored, today=TODAY) == stored.date()
    assert feed.reported_on(day(-60), None, stored, today=TODAY) == day(-60)


def test_a_future_publish_date_is_not_a_report_date():
    """Publisher metadata is sometimes wrong; a date after today falls through."""
    assert feed.reported_on(day(30), day(-3), NOW, today=TODAY) == day(-3)
    assert feed.reported_on(None, day(400), NOW, today=TODAY) == TODAY


def test_a_fact_written_long_after_its_page_was_fetched_is_new_when_written(session):
    """The bug that lost updates: enrichment re-reads cached pages, so a row written
    today can carry a fetch from weeks ago. Its insert time is what is new."""
    project = _project(session)
    _event(session, project, created_at=BEFORE, recorded_at=NOW)

    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.at == NOW
    assert signal.key.startswith("event:")


def test_a_cleared_obstacle_is_placed_on_the_day_it_was_resolved(session):
    """A resolution the source dates seven weeks ago, recorded today, is in the
    two-month window and not in this week's."""
    project = _project(session)
    _risk(
        session,
        project,
        status="resolved",
        resolved_at=day(-52),
        closed_at=NOW,
        created_at=BEFORE,
    )
    assert feed.digest(session, since=SINCE).signals == ()
    [signal] = feed.digest(session, days=60).signals
    assert signal.kind == "obstacle_cleared"
    assert signal.at == NOW and signal.reported == day(-52)
    assert signal.key.endswith(":cleared")


def test_an_event_with_no_discovery_date_is_placed_by_its_own_date(session):
    """NULL means "we do not know when we learned this" (migration 0018), which no
    longer matters: what places it is when it was reported."""
    project = _project(session)
    _event(session, project, created_at=None)
    [signal] = feed.digest(session, since=SINCE).signals
    assert signal.reported == day(-1)


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
    """A row closed before 0029 has no `closed_at`, so `resolved_at` stands in for it."""
    project = _project(session)
    _risk(
        session,
        project,
        status="resolved",
        resolved_at=day(-1),
        # Learned long before the window: it is the resolution that is new.
        created_at=BEFORE,
    )
    [signal] = feed.digest(session, since=SINCE).signals
    assert (signal.kind, signal.sign) == ("obstacle_cleared", "good")
    assert signal.happened == day(-1)


def test_a_resolved_obstacle_outside_the_window_is_silent(session):
    project = _project(session)
    _risk(session, project, status="resolved", resolved_at=day(-52), created_at=BEFORE)
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
        event_date=day(-1),
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
    _source(session, project, when=dt.datetime.combine(day(-3), dt.time(3, 0)))
    assert feed.digest(session, since=SINCE).last_crawl == dt.datetime.combine(
        day(-3), dt.time(3, 0)
    )


# --- scope -----------------------------------------------------------------


def test_with_no_watchlist_the_whole_database_is_read(session):
    _project(session, created_at=NOW)
    result = feed.digest(session, since=SINCE)
    assert result.watching_everything
    assert len(result.signals) == 1


def test_a_watchlist_scopes_the_digest(session, account):
    watched = _project(session, created_at=NOW)
    _project(session, company="Meta", name="Hyperion", state="LA", dedup_key="m", created_at=NOW)

    watchlist.add(session, "xAI", account_id=account.id)
    result = feed.digest(session, since=SINCE)
    assert not result.watching_everything
    assert [s.project_id for s in result.signals] == [watched.id]
    assert result.projects_watched == 1


def test_each_watched_entity_gets_its_own_tally(session, account):
    xai = _project(session)
    meta = _project(session, company="Meta", name="Hyperion", state="LA", dedup_key="m")
    _event(session, xai, event_type="energized", description="Energized.")
    _risk(session, meta)
    _event(
        session,
        meta,
        event_type="delayed",
        event_date=day(-1),
        description="Slipped a year.",
        quote=None,
        unconfirmed="no_quote",
    )

    watchlist.add(session, "xAI", account_id=account.id)
    watchlist.add(session, "Meta", account_id=account.id)
    result = feed.digest(session, since=SINCE)

    by_entry = {e.entry: e for e in result.entities}
    assert (by_entry["xAI"].good, by_entry["xAI"].bad) == (1, 0)
    assert (by_entry["Meta"].good, by_entry["Meta"].bad) == (0, 1)
    # The unconfirmed slip is counted as held rather than as news.
    assert by_entry["Meta"].held == 1


def test_a_signal_names_the_watch_that_brought_it_in(session, account):
    project = _project(session, company="Crusoe", customer="OpenAI", name="Abilene", dedup_key="c")
    _event(session, project, description="Energized.")

    watchlist.add(session, "OpenAI", account_id=account.id)
    [signal] = feed.digest(session, since=SINCE).signals
    assert (signal.entry, signal.via) == ("OpenAI", watchlist.VIA_CUSTOMER)


def test_the_default_window_is_a_week_and_no_window_reaches_past_two_months(session):
    """`days` and an explicit `since` are the same knob, and neither can reach back
    further than `REPORT_WINDOW_DAYS`."""
    project = _project(session)
    _event(session, project, event_date=day(-2), created_at=NOW)
    _event(
        session,
        project,
        event_type="first_customer",
        event_date=day(-feed.REPORT_WINDOW_DAYS - 5),
        created_at=NOW,
    )
    assert [s.label for s in feed.digest(session).signals] == ["energized"]
    assert feed.digest(session, days=1).signals == ()
    assert [s.label for s in feed.digest(session, days=365).signals] == ["energized"]
    assert [s.label for s in feed.digest(session, since=dt.datetime(2020, 1, 1)).signals] == [
        "energized"
    ]


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
        event_date=day(-30),
        description="AWS withdrew CUP application amid neighbour opposition.",
    )
    _event(
        session,
        project,
        event_type="delayed",
        event_date=day(-20),
        description="AWS withdraws CUP application.",
    )

    [signal] = feed.digest(session, days=60).signals
    assert signal.restatements == 1
    # The database still holds both rows; only the digest folds them.
    assert len(project.events) == 2


def test_folding_does_not_hide_a_quoted_signal_behind_an_unquoted_one(session):
    project = _project(session)
    _event(session, project, event_date=day(-1), description="Energized.")
    _event(
        session,
        project,
        event_date=day(-2),
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
        event_date=day(-1),
        description="Agreement signed.",
    )
    [signal] = [s for s in feed.digest(session, since=SINCE).signals if s.label != "grid_capacity"]
    assert signal.notify


def test_a_decisive_milestone_notifies_and_a_cheap_one_does_not(session):
    """The five things worth a notification, and the ones that are page-only.

    Dated days before today, not on fixed dates: a milestone reported more than
    `REPORT_WINDOW_DAYS` ago is stale and never notifies, so fixed August dates made
    this fail from 2026-10-03 on for a reason that had nothing to do with kind.
    """
    project = _project(session)
    for kind, date in (
        ("energized", day(-1)),
        ("first_customer", day(-2)),
        ("delayed", day(-3)),
        ("announced", day(-4)),
        ("permit_filed", day(-5)),
        ("land_acquired", day(-6)),
        ("site_work", day(-7)),
    ):
        _event(
            session,
            project,
            event_type=kind,
            event_date=date,
            description=f"{kind}.",
        )

    by_label = {s.label: s for s in feed.digest(session, days=30).signals}
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
        resolved_at=day(-1),
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
        event_date=day(-2),
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
        resolved_at=day(-1),
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
        resolved_at=day(-1),
        created_at=BEFORE,
    )
    produced = {s.kind for s in feed.digest(session, since=SINCE).signals}
    assert produced == set(feed.KINDS)


def test_a_chips_tally_matches_the_cards_that_chip_filters_to(session, account):
    """The number above the list and the list have to agree.

    They did not. Tallies were counted from the unfolded signals while the card
    list was folded, so one moment reported by three publishers was three updates
    in the chip and one card underneath it — measured live at 134 against 41.
    """
    project = _project(session)
    # One moment, three publishers: same project, same kind, same label.
    for i in range(3):
        citation = _source(session, project, url=f"https://trade.example/{i}")
        _event(session, project, source_id=citation.id, event_date=day(-4 + i))
    watchlist.add(session, "xAI", account_id=account.id)

    got = feed.digest(session, since=SINCE)

    tally = {e.entry: e for e in got.entities}["xAI"]
    cards = [s for s in got.signals if s.entry == "xAI"]
    assert len(cards) == 1, "three articles, one moment"
    assert cards[0].restatements == 2
    assert tally.total == len(cards), "the chip must count what clicking it shows"
    assert tally.good == 1


def test_the_page_limit_does_not_shrink_the_tally(session, account):
    """The chip describes the window; the limit describes the page."""
    project = _project(session)
    for i, kind in enumerate(("energized", "land_acquired", "permit_approved")):
        citation = _source(session, project, url=f"https://trade.example/{i}")
        _event(session, project, event_type=kind, source_id=citation.id)
    watchlist.add(session, "xAI", account_id=account.id)

    got = feed.digest(session, since=SINCE, limit=1)

    assert len(got.signals) == 1
    assert {e.entry: e for e in got.entities}["xAI"].total == 3


# --- the recency gate: it has to have happened recently, not just been learned ---
#
# The window is on `created_at`, and a crawl imports a project's whole back-history
# at once, so "we learned it last night" says nothing about when it happened.
# Measured on the live database over 30 days: of 354 signals that would have
# notified, 107 described something more than three years old.


def _notifiable(**kw):
    """A signal that clears the other three gates, so each test moves one thing."""
    base = {
        "kind": "milestone",
        "sign": "good",
        "project_id": 1,
        "company": "Nscale",
        "project": "Monarch Compute Campus",
        "label": "energized",
        "detail": "Powered up.",
        "weight": feed.NOTIFY_WEIGHT,
    }
    base.update(kw)
    return feed.Signal(**base)


def test_a_milestone_reported_years_ago_does_not_interrupt_anybody():
    """The defect this gate exists for: an article read last night carrying a 2021
    groundbreaking cleared confirmed, not-future and material, and paged somebody
    about 2021."""
    old = _notifiable(reported=TODAY - dt.timedelta(days=1100))
    assert feed.stale(old)
    assert not feed.notable(old)


def test_a_recently_reported_milestone_still_does():
    fresh = _notifiable(reported=TODAY - dt.timedelta(days=3))
    assert not feed.stale(fresh)
    assert feed.notable(fresh)


def test_background_never_notifies():
    assert not feed.notable(_notifiable(reported=TODAY, background=True))


def test_the_horizon_is_where_it_says_it_is():
    """A boundary worth pinning: the constant is the documented contract."""
    edge = TODAY - dt.timedelta(days=feed.REPORT_WINDOW_DAYS)
    assert feed.notable(_notifiable(reported=edge))
    assert not feed.notable(_notifiable(reported=edge - dt.timedelta(days=1)))


def test_an_undated_signal_is_kept():
    """No report date at all says nothing about age, and an open obstacle is a
    statement about now. Treating "no date" as "old" would silently drop the live
    risks this channel exists to carry."""
    undated = _notifiable(kind="obstacle_opened", sign="bad", reported=None)
    assert not feed.stale(undated)
    assert feed.notable(undated)


def test_the_gate_does_not_rescue_what_the_other_three_refused():
    """Recency is a fourth gate, not a replacement. A fresh but unconfirmed or
    immaterial signal still says nothing."""
    assert not feed.notable(_notifiable(reported=TODAY, unconfirmed="no_quote"))
    assert not feed.notable(_notifiable(reported=TODAY, weight=feed.NOTIFY_WEIGHT - 1))
    assert not feed.notable(
        _notifiable(happened=TODAY + dt.timedelta(days=400), reported=TODAY, expected=True)
    )


def test_the_page_and_the_email_agree_about_history(session):
    """They used to split: the page carried a three-year-old milestone the crawler
    had just found, with both dates on it, and only the email held it back. The
    product's rule is that it is not an update anywhere."""
    project = Project(
        name="Monarch Compute Campus",
        company="Nscale",
        county="Mason",
        state="WV",
        dedup_key="nscale|county:mason|WV",
        phase="construction",
        confidence=2,
    )
    session.add(project)
    session.flush()
    session.add(
        Event(
            project_id=project.id,
            event_date=dt.date.today() - dt.timedelta(days=1200),
            event_type="groundbreaking",
            description="Ground was broken.",
            created_at=dt.datetime.now(),
        )
    )
    session.flush()

    brief = feed.digest(session, days=60)
    assert not [s for s in brief.signals if s.kind == "milestone"]
    assert not brief.notifying


# --- an empty watchlist means nothing, not everything ---------------------------


def test_an_empty_watchlist_watches_nothing(session, account):
    """The default 0022 inverted.

    It used to mean *everything*, so "watching" depended on a row count nobody
    could see: two accounts that had asked for nothing were shown all 456 projects
    and their pages were indistinguishable from a watchlist that had leaked
    between them.
    """
    _project(session, created_at=NOW)
    _project(session, company="Meta", name="Hyperion", state="LA", dedup_key="m", created_at=NOW)

    result = feed.digest(session, since=SINCE, account_id=account.id)

    assert not result.watching_everything
    assert result.projects_watched == 0
    assert result.signals == ()


def test_watch_all_takes_the_whole_database_back(session, account):
    """And the button that turns it on is the point: wanting all of it is a
    legitimate thing to want, it just has to be asked for."""
    _project(session, created_at=NOW)
    _project(session, company="Meta", name="Hyperion", state="LA", dedup_key="m", created_at=NOW)
    account.watch_all = True
    session.flush()

    result = feed.digest(session, since=SINCE, account_id=account.id)

    assert result.watching_everything
    assert result.projects_watched == 2
    assert result.signals


def test_two_accounts_with_empty_lists_both_see_nothing(session):
    """The symptom that prompted the change: two people who had asked for nothing
    saw identical full pages, which reads exactly like a leak."""
    from tracker import accounts

    a = accounts.create(session, "a@example.com", "correct horse battery")
    b = accounts.create(session, "b@example.com", "correct horse battery")
    _project(session, created_at=NOW)

    for who in (a, b):
        result = feed.digest(session, since=SINCE, account_id=who.id)
        assert result.projects_watched == 0, f"{who.email} should watch nothing"
        assert not result.watching_everything


def test_no_account_named_keeps_the_old_fallback(session, account):
    """`tracker digest` with no `--user` asks for every account's view, and a
    console with no accounts has nobody whose preference to read. Neither is a
    person saying "nothing"; there is simply no person."""
    _project(session, created_at=NOW)

    result = feed.digest(session, since=SINCE)

    assert result.watching_everything
    assert result.projects_watched == 1
