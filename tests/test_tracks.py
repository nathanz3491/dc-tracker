"""Where a project stands, per track, and what would prove it is moving.

This module is the PRD's central ask -- 判断一个项目究竟走到了哪一步 -- so these
tests are written as the claims that ask makes:

* a project can be far along on one track and stuck on another, which a single
  `phase` enum cannot express;
* the obstacle taxonomy and the stage ladder are the same five tracks, so every
  risk category must land on one;
* "what signal proves it is advancing" is the next unreached milestone on the
  blocked track, not an opinion.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tracker.tracks import (
    RISK_TRACK,
    TRACK_LABELS,
    TRACK_MILESTONES,
    TRACKS,
    UNKNOWN,
    standing,
)
from tracker.vocab import EVENT_TYPES, RISK_CATEGORIES


def ev(event_type: str):
    return SimpleNamespace(event_type=event_type)


def risk(category: str, severity: str = "material", status: str = "open"):
    return SimpleNamespace(category=category, severity=severity, status=status)


# --- the vocabulary lines up -------------------------------------------------


def test_every_risk_category_lands_on_a_track():
    """The PRD's obstacle list and its stage ladder are the same five tracks.

    If a category had no track, `binding_blocker` would silently ignore that
    obstacle and the project would read as unblocked.
    """
    unplaced = [c for c in RISK_CATEGORIES if c != "unclassified" and c not in RISK_TRACK]
    assert unplaced == [], f"these obstacles block no track: {unplaced}"


def test_every_milestone_belongs_to_exactly_one_track():
    placed = [m for ms in TRACK_MILESTONES.values() for m in ms]
    assert len(placed) == len(set(placed)), "a milestone on two tracks would double-count"
    for milestone in placed:
        assert milestone in EVENT_TYPES, f"{milestone} is not a storable event type"


def test_every_track_has_a_label_and_milestones():
    for track in TRACKS:
        assert track in TRACK_LABELS
        assert TRACK_MILESTONES[track], f"{track} has no milestones to reach"


@pytest.mark.parametrize("track", ["power", "permits", "construction", "commercial"])
def test_risk_track_targets_are_real_tracks(track):
    assert track in TRACKS


# --- per-track position ------------------------------------------------------


def test_a_project_can_be_advanced_on_one_track_and_untouched_on_another():
    """The whole reason this module exists.

    Land bought and ground broken, but no interconnection agreement — to a single
    `phase` enum this is just "construction", which hides the thing that decides
    whether it ever runs.

    Permits are not asserted here: breaking ground implies an approved permit (see
    `IMPLIED_BY`), so power is the track that genuinely stays open.
    """
    stand = standing(1, [ev("announced"), ev("land_acquired"), ev("groundbreaking")], [])

    assert stand.track("site_control").complete
    assert stand.track("construction").status == "groundbreaking"
    assert stand.track("power").status == UNKNOWN, "built is not energised"
    assert stand.track("permits").complete, "you cannot break ground unpermitted"


def test_a_track_reports_its_latest_milestone_not_its_first():
    stand = standing(1, [ev("permit_filed"), ev("permit_approved")], [])
    assert stand.track("permits").status == "permit_approved"
    assert stand.track("permits").complete


def test_filing_a_permit_is_not_approving_one():
    """Often years apart, and only the second is progress."""
    stand = standing(1, [ev("permit_filed")], [])
    permits = stand.track("permits")
    assert permits.status == "permit_filed"
    assert not permits.complete
    assert permits.next_milestone == "permit_approved"


def test_events_out_of_order_still_read_as_the_ladder_order():
    """Articles are read in whatever order they were queued."""
    stand = standing(1, [ev("groundbreaking"), ev("site_work")], [])
    assert stand.track("construction").reached == ("site_work", "groundbreaking")


def test_an_unrelated_event_type_moves_no_track():
    stand = standing(1, [ev("delayed"), ev("expanded")], [])
    assert all(t.status == UNKNOWN for t in stand.tracks)


# --- blockers ----------------------------------------------------------------


def test_a_risk_blocks_the_track_it_belongs_to():
    stand = standing(1, [ev("announced")], [risk("grid_capacity")])
    assert stand.track("power").is_blocked
    assert stand.track("permits").is_blocked is False
    assert [t.track for t in stand.blocked] == ["power"]


def test_a_resolved_risk_is_history_not_a_blocker():
    stand = standing(1, [], [risk("transmission", status="resolved")])
    assert stand.blocked == ()
    assert stand.binding_blocker is None


def test_an_unclassified_risk_blocks_nothing():
    """It cannot be placed on a track, so it must not be guessed onto one."""
    stand = standing(1, [], [risk("unclassified")])
    assert stand.blocked == ()


def test_severity_decides_which_blocker_binds():
    stand = standing(
        1,
        [],
        [risk("permitting", severity="watch"), risk("grid_capacity", severity="blocking")],
    )
    binding = stand.binding_blocker
    assert binding is not None
    assert binding.track == "power", "a blocking risk outranks a watch-level one"


def test_at_equal_severity_the_earlier_track_binds():
    """A project stuck on permits AND short of a customer is stuck on permits.

    It cannot reach the later problem until the earlier one clears.
    """
    stand = standing(
        1, [], [risk("permitting", severity="material"), risk("offtake", severity="material")]
    )
    assert stand.binding_blocker.track == "permits"


def test_a_tracks_severity_is_its_worst_risk():
    stand = standing(
        1,
        [],
        [risk("grid_capacity", severity="watch"), risk("transmission", severity="blocking")],
    )
    assert stand.track("power").blocker_severity == "blocking"


def test_several_risks_on_one_track_are_all_listed_once():
    stand = standing(1, [], [risk("water"), risk("community_opposition"), risk("water")])
    assert stand.track("permits").blockers == ("water", "community_opposition")


# --- the PRD's final question ------------------------------------------------


def test_the_signal_to_watch_comes_from_the_blocked_track():
    """接下来出现什么信号 — answered structurally, not by opinion."""
    stand = standing(1, [ev("announced"), ev("land_acquired")], [risk("grid_capacity")])
    assert stand.watch_for is not None
    assert "interconnection" in stand.watch_for


def test_with_nothing_blocked_the_signal_is_the_next_step_anyway():
    """A project progressing normally still has a next milestone worth watching."""
    stand = standing(1, [ev("announced")], [])
    assert stand.binding_blocker is None
    assert stand.watch_for is not None
    assert "deed" in stand.watch_for or "land" in stand.watch_for


def test_a_finished_project_on_every_track_has_nothing_to_watch():
    every = [ev(m) for ms in TRACK_MILESTONES.values() for m in ms]
    stand = standing(1, every, [])
    assert all(t.complete for t in stand.tracks)
    assert stand.watch_for is None


def test_every_incomplete_milestone_has_a_signal_written_for_it():
    """A missing entry would leave `watch_for` silently None."""
    from tracker.tracks import NEXT_SIGNAL

    for track, ladder in TRACK_MILESTONES.items():
        for milestone in ladder:
            if milestone == "announced":
                continue  # nothing precedes it, so it is never a "next" step
            assert milestone in NEXT_SIGNAL, f"{track}/{milestone} has no signal to watch for"


def test_a_later_milestone_implies_the_earlier_ones():
    """Regression, caught by reading real output for project 1.

    Its power track was `energized` and the report asked it to go and obtain an
    interconnection agreement. Milestones are cumulative: an energized site plainly
    has one, whether or not any article we happened to read said so.
    """
    stand = standing(1, [ev("energized")], [])
    power = stand.track("power")
    assert power.status == "energized"
    assert power.complete, "nothing further to reach on this track"
    assert power.next_milestone is None
    assert power.next_signal is None


def test_a_skipped_middle_milestone_does_not_become_the_next_step():
    stand = standing(1, [ev("equipment_install")], [])
    construction = stand.track("construction")
    assert construction.complete
    assert construction.next_milestone is None


def test_furthest_track_summarises_progress():
    stand = standing(1, [ev("announced"), ev("site_work")], [])
    assert stand.furthest_track == "construction"
    assert standing(1, [], []).furthest_track is None


def test_a_project_with_no_evidence_reads_as_unknown_not_as_finished():
    stand = standing(1, [], [])
    assert all(t.status == UNKNOWN for t in stand.tracks)
    assert not any(t.complete for t in stand.tracks)


# --- cross-track implication -------------------------------------------------


def test_a_built_project_implies_land_and_permits():
    """Reading real output made this obvious.

    Sabey Ashburn showed construction `complete` beside three `unknown` tracks,
    which reads as a data fault rather than a finding. You cannot pour a building on
    land you do not control, nor break ground without an approved permit.
    """
    stand = standing(1, [ev("equipment_install")], [])

    assert stand.track("site_control").complete
    assert stand.track("permits").complete
    assert stand.track("construction").complete


def test_construction_does_not_imply_power_and_that_is_the_point():
    """Building ahead of power is routine and is THE bottleneck of this cycle.

    A finished shell waiting on a substation is exactly what this tracker exists to
    surface. Implying an interconnection agreement from construction would erase the
    most valuable signal in the dataset.
    """
    stand = standing(1, [ev("equipment_install")], [])
    power = stand.track("power")

    assert power.status == UNKNOWN, "a built site is not necessarily an energised one"
    assert not power.complete
    assert stand.watch_for is not None
    assert "interconnection" in stand.watch_for


def test_construction_does_not_imply_a_customer():
    """Speculative builds with no signed tenant are common."""
    stand = standing(1, [ev("groundbreaking")], [])
    assert stand.track("commercial").status == UNKNOWN


def test_an_energised_site_implies_permits_and_land():
    stand = standing(1, [ev("energized")], [])
    assert stand.track("site_control").complete
    assert stand.track("permits").complete
    assert stand.track("power").complete


def test_an_implied_milestone_is_distinguishable_from_a_reported_one():
    """A deduction is not a citation, and the row must be able to say which."""
    implied = standing(1, [ev("groundbreaking")], []).track("permits")
    assert implied.only_implied
    assert "permit_approved" in implied.implied

    reported = standing(1, [ev("groundbreaking"), ev("permit_approved")], []).track("permits")
    assert not reported.only_implied, "an event we actually read is not implied"
    assert "permit_approved" not in reported.implied


def test_implication_never_invents_a_milestone_out_of_nothing():
    stand = standing(1, [], [])
    assert all(t.status == UNKNOWN for t in stand.tracks)
    assert all(not t.implied for t in stand.tracks)


def test_every_implication_names_real_milestones():
    """A typo in IMPLIED_BY would silently imply nothing."""
    from tracker.tracks import IMPLIED_BY

    known = {m for ms in TRACK_MILESTONES.values() for m in ms}
    for trigger, implied in IMPLIED_BY.items():
        assert trigger in known, f"{trigger} is on no track"
        for milestone in implied:
            assert milestone in known, f"{trigger} implies unknown milestone {milestone}"


def test_implication_is_not_circular():
    """A milestone must not imply itself, directly or via its own ladder."""
    from tracker.tracks import IMPLIED_BY

    for trigger, implied in IMPLIED_BY.items():
        assert trigger not in implied, f"{trigger} implies itself"
