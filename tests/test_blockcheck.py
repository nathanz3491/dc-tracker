"""Finding blocks that are one tranche under several names.

Pure and offline: `group_blocks` takes anything shaped like a `CapacityBlock`, so
these are plain stubs and no database is needed.

The labels are the real ones from Fairwater (#1) and Hyperion (#10) — 25 and 26
sources respectively — because the failure this module exists for only appears
once many sources have each named the same building their own way.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tracker.blockcheck import UNKNOWN_CLASS, families, group_blocks, segment_family


@dataclass
class Blk:
    """A stand-in for `CapacityBlock`, with only what grouping reads.

    `block_key` is derived with the real `blocks.block_key` rather than defaulted
    to a constant. It used to be the literal `"k"` for every stub, which made all
    of them share one identity — and `sections` dedupes ungrouped blocks on that
    key, so three distinct buildings collapsed into one. The schema's
    `UNIQUE(project_id, block_key)` means that cannot happen to real rows, so the
    fixture was lying rather than the code being wrong.
    """

    id: int
    label: str
    status: str = "under_construction"
    mw: float | None = None
    unconfirmed_fields: str | None = None
    parent: str | None = None
    block_key: str = ""
    source_id: int | None = 1
    energized_on: object = None
    expected_online: object = None
    customer: str | None = None
    generic: bool = False

    def __post_init__(self) -> None:
        if not self.block_key:
            from tracker.blocks import block_key

            self.block_key = block_key(self.label, self.parent).value


def verdicts(blocks):
    return {g.family: g.verdict for g in group_blocks(1, blocks)}


# --- the designator family --------------------------------------------------


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("building-2", ("structure", "2")),
        ("area-2", ("structure", "2")),
        ("phase-1", ("phase", "1")),
        ("hall-3", ("hall", "3")),
        ("mke-4", ("named:mke", "4")),
        ("1-2", (UNKNOWN_CLASS, "2")),
        # No ordinal: a place, not a numbered slice of one.
        ("fairwater", None),
        ("original", None),
    ],
)
def test_segment_family(segment, expected):
    assert segment_family(segment) == expected


def test_a_capacity_in_the_label_is_not_an_ordinal():
    """`_fold` turns "1.2" into "1 2", so "Initial 1.2 GW Phase" was claiming
    ordinal **2** and offering itself as a reading of Building 2.

    The capacity goes and the genuine ordinal stays: "Initial Phase" is phase 1.
    """
    assert families(_member("Initial 1.2 GW Phase")) == {("phase", "1")}
    assert (UNKNOWN_CLASS, "2") not in families(_member("Initial 1.2 GW Phase"))
    # A size with no ordinary designator claims nothing at all.
    assert families(_member("8 MW expansion")) == set()
    assert (UNKNOWN_CLASS, "2") not in families(_member("Phase 1, 1.2 GW"))


def _member(label: str):
    from tracker.blockcheck import Member

    return Member.of(Blk(id=1, label=label))


# --- the live Fairwater case -------------------------------------------------

#: Fairwater's four names for one building under construction, as stored.
FAIRWATER_SECOND = [
    Blk(id=1, label="Building 2", source_id=7),
    Blk(id=2, label="Facility 2", parent="Fairwater Datacenter", source_id=674),
    Blk(id=3, label="Second facility", source_id=675),
    Blk(id=4, label="Area II", source_id=680),
]


def test_synonyms_for_one_building_group_together():
    """building / facility / area are one kind of thing, and `blocks.py` treats
    them three different ways — kept, deleted, and mistaken for a place name."""
    groups = group_blocks(1, FAIRWATER_SECOND)
    assert len(groups) == 1
    assert groups[0].family == "structure-2"
    assert groups[0].verdict == "mergeable"
    assert {m.label for m in groups[0].members} == {
        "Building 2",
        "Facility 2",
        "Second facility",
        "Area II",
    }


def test_a_bare_ordinal_between_two_families_is_ambiguous_not_assigned():
    """ "Facility 1" reduces to `1`, which fits Building 1 and fits Phase 1.

    Guessing is how a phase's 400 MW gets folded into a building.
    """
    blocks = [
        Blk(id=1, label="Building 1", status="serving"),
        Blk(id=2, label="Phase 1", status="energized", mw=400.0),
        Blk(id=3, label="Facility 1", parent="Fairwater Datacenter", status="serving"),
        Blk(id=4, label="First datacenter facility", status="serving"),
    ]
    groups = group_blocks(1, blocks)
    ambiguous = [g for g in groups if g.verdict == "ambiguous"]
    assert len(ambiguous) == 1
    assert {m.label for m in ambiguous[0].members} == {"Facility 1", "First datacenter facility"}
    assert set(ambiguous[0].ambiguous_with) == {"Building 1", "Phase 1"}


def test_one_family_absorbs_the_bare_ordinal():
    """With no rival family, the bare ordinal is not a question at all."""
    blocks = [
        Blk(id=1, label="Building 1", status="serving"),
        Blk(id=2, label="First datacenter facility", status="serving"),
    ]
    groups = group_blocks(1, blocks)
    assert [g.verdict for g in groups] == ["mergeable"]
    assert len(groups[0].members) == 2


def test_a_named_family_may_not_claim_a_bare_ordinal():
    """ "Campus Two (International Drive)" is numbered 2, but "Second facility"
    cannot mean Durand Avenue — letting named families compete made two
    resolvable groups ambiguous."""
    blocks = [
        Blk(id=1, label="Building 2"),
        Blk(id=2, label="Second facility"),
        Blk(id=3, label="Campus Two (International Drive)", status="planned"),
    ]
    groups = group_blocks(1, blocks)
    assert [g.family for g in groups] == ["structure-2"]
    assert {m.label for m in groups[0].members} == {"Building 2", "Second facility"}


# --- the rule that protects the totals --------------------------------------


def test_two_confirmed_capacities_refuse_to_merge():
    """The live Hyperion case: Phase 1 is 2,000 MW and "Phase 1 IT Load" is
    1,500. Those are facility load and IT load, two measurements of one phase.

    Picking a winner silently is how mw_built MAX put 13,620 MW on that row.
    """
    blocks = [
        Blk(id=1, label="Phase 1", mw=2000.0),
        Blk(id=2, label="Initial Phase", mw=2000.0),
        Blk(id=3, label="Phase 1 IT Load", mw=1500.0, status="planned"),
    ]
    groups = group_blocks(10, blocks)
    assert len(groups) == 1
    assert groups[0].verdict == "collides"
    mw = next(c for c in groups[0].conflicts if c.field == "mw")
    assert mw.confirmed_both_ways is True


def test_an_unconfirmed_capacity_does_not_make_a_collision():
    """待确认 against a quoted figure is the tier working, not a contradiction —
    and `reconcile` already keeps the unconfirmed one out of the total."""
    blocks = [
        Blk(id=1, label="Phase 1", mw=400.0),
        Blk(id=2, label="Initial phase", mw=350.0, unconfirmed_fields="mw"),
    ]
    groups = group_blocks(1, blocks)
    assert groups[0].verdict == "mergeable"


def test_differing_dates_are_reported_without_blocking_a_merge():
    """Two commissioning dates for one building is worth seeing, and is not a
    reason to refuse: April versus June is exactly the Fairwater question."""
    import datetime as dt

    blocks = [
        Blk(id=1, label="Building 1", status="serving", energized_on=dt.date(2026, 4, 1)),
        Blk(id=2, label="First facility", status="serving", energized_on=dt.date(2026, 5, 1)),
    ]
    groups = group_blocks(1, blocks)
    assert groups[0].verdict == "mergeable"
    assert any(c.field == "energized_on" for c in groups[0].conflicts)


# --- what must never be grouped ---------------------------------------------


def test_different_classes_are_containment_not_duplication():
    """A hall sits inside a building, built during a phase. Same ordinal, three
    different things."""
    blocks = [
        Blk(id=1, label="Building 1"),
        Blk(id=2, label="Phase 1"),
        Blk(id=3, label="Hall 1"),
    ]
    assert group_blocks(1, blocks) == []


def test_different_stems_never_merge():
    """AZP-2 and VA-2 share an ordinal and nothing else."""
    assert group_blocks(1, [Blk(id=1, label="AZP-2"), Blk(id=2, label="VA-2")]) == []


def test_labels_without_an_ordinal_are_left_alone():
    """Whether "Original Data Center" is the second building is a judgement no
    string folding settles, so it is not raised as one."""
    blocks = [
        Blk(id=1, label="Fairwater"),
        Blk(id=2, label="Wisconsin campus"),
        Blk(id=3, label="Original Data Center"),
    ]
    assert group_blocks(1, blocks) == []


def test_a_single_block_is_not_a_group():
    assert group_blocks(1, [Blk(id=1, label="Building 2")]) == []


# --- the campus as sections, which is what the table shows ------------------


def test_sections_collapse_the_duplicate_namings():
    """The live Polaris Forge case: seven blocks are four buildings."""
    from tracker.blockcheck import sections

    blocks = [
        Blk(id=1, label="Building 2 (ELN-02)", status="serving", mw=100.0),
        Blk(id=2, label="Building 2", status="serving", mw=100.0),
        Blk(id=3, label="Building 3 (ELN-03)", status="under_construction", mw=150.0),
        Blk(id=4, label="Building 3", status="under_construction", mw=150.0),
        Blk(id=5, label="HPC Facility", status="under_construction", mw=100.0),
    ]
    secs = sections(135, blocks)
    assert [s.label for s in secs] == ["Building 2", "Building 3", "HPC Facility"]
    assert secs[0].aliases == ("Building 2 (ELN-02)",)


def test_a_section_says_what_it_delivers_of_what_it_holds():
    """The question the flat list could not answer: 0 of 150, not just 150."""
    from tracker.blockcheck import sections

    live, building = sections(
        1,
        [
            Blk(id=1, label="Building 1", status="serving", mw=100.0),
            Blk(id=2, label="Building 2", status="under_construction", mw=150.0),
        ],
    )
    assert (live.delivering, live.capacity) == (100.0, 100.0)
    assert (building.delivering, building.capacity) == (0.0, 150.0)


def test_sections_are_ordered_by_identity_not_by_state():
    """The defect this replaced: a site plan sorted by how far along things are.

    Building 1 comes before Building 2 even though Building 2 is further along.
    """
    from tracker.blockcheck import sections

    secs = sections(
        1,
        [
            Blk(id=1, label="Building 2", status="serving", mw=100.0),
            Blk(id=2, label="Building 1", status="planned", mw=50.0),
            Blk(id=3, label="Phase 1", status="energized", mw=25.0),
        ],
    )
    # Structures before phases, and within a class by ordinal.
    assert [s.label for s in secs] == ["Building 1", "Building 2", "Phase 1"]


def test_a_section_never_picks_between_two_confirmed_capacities():
    from tracker.blockcheck import sections

    (sec,) = sections(
        10,
        [
            Blk(id=1, label="Phase 1", mw=2000.0),
            Blk(id=2, label="Phase 1 IT Load", mw=1500.0),
        ],
    )
    assert sec.capacity_conflict == (1500.0, 2000.0)
    assert sec.verdict == "collides"


def test_the_canonical_name_is_the_one_that_says_what_the_thing_is():
    """ "Building 2" over "Area II" and "Second facility": a class word plus a digit."""
    from tracker.blockcheck import sections

    (sec,) = sections(1, FAIRWATER_SECOND)
    assert sec.label == "Building 2"
    assert set(sec.aliases) == {"Area II", "Facility 2", "Second facility"}


def test_an_unconfirmed_capacity_is_carried_but_flagged():
    from tracker.blockcheck import sections

    (sec,) = sections(1, [Blk(id=1, label="Building 1", mw=350.0, unconfirmed_fields="mw")])
    assert sec.capacity == 350.0
    assert sec.capacity_confirmed is False
