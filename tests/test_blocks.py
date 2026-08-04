"""Capacity blocks: identity, and the rollup that must never lose information.

Two things carry the whole design and both are tested here rather than trusted.

**Identity.** `block_key` has to make two sources agree without ever guessing. A
filing writing "AZP-3 Phase 3" and an article writing "Phase 3" of AZP-3 are one
tranche; "Phase 1" and "AZP-2" are not, however tempting the similarity.

**Monotonicity.** `rollup` may only ever raise a project scalar. A block sum is a
floor on the campus, not a replacement for a cited total, and the 227 existing rows
depend on that being true.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tracker import blocks
from tracker.vocab import (
    BLOCK_LIVE,
    BLOCK_PROGRESSION,
    BLOCK_STATUS_TO_PHASE,
    BLOCK_STATUSES,
    BLOCK_TERMINAL,
    PHASES,
)

# --- vocabulary -------------------------------------------------------------


def test_the_status_map_is_total_and_lands_inside_the_project_phases():
    """A block status with no phase would silently drop out of the rollup."""
    assert set(BLOCK_STATUS_TO_PHASE) == set(BLOCK_STATUSES)
    assert set(BLOCK_STATUS_TO_PHASE.values()) <= set(PHASES)


def test_the_two_distinctions_blocks_exist_for_are_present():
    """`shell_complete`, and the split between power on and a customer running.

    These are the states the project `phase` enum cannot express, and the reason
    this vocabulary is separate rather than reusing `PHASE_PROGRESSION`.
    """
    assert "shell_complete" in BLOCK_PROGRESSION
    assert "energized" in BLOCK_PROGRESSION
    assert "serving" in BLOCK_PROGRESSION
    assert BLOCK_PROGRESSION.index("shell_complete") < BLOCK_PROGRESSION.index("energized")
    assert BLOCK_PROGRESSION.index("energized") < BLOCK_PROGRESSION.index("serving")
    # Both mean megawatts are delivering, which is what `mw_built` sums.
    assert {"energized", "serving"} == BLOCK_LIVE


def test_terminal_statuses_sit_outside_the_progression():
    """Same reasoning as `PHASE_TERMINAL`: cancelled is not further along."""
    assert not set(BLOCK_TERMINAL) & set(BLOCK_PROGRESSION)


# --- identity ---------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    ["Phase 1", "Phase I", "phase one", "first phase", "The first phase", "PHASE 1"],
)
def test_every_spelling_of_a_phase_is_one_block(label):
    """Filings write roman, trade press writes words, and both mean one tranche.

    Without this the same phase arrives as five blocks and their megawatts are
    summed five times.
    """
    assert blocks.block_key(label).value == "phase-1"


def test_a_parent_makes_two_sources_agree_on_one_block():
    """Project 39, exactly.

    An SEC filing calls it "AZP-3 Phase 3"; an article about AZP-3 calls it
    "Phase 3". One tranche of one campus, and the key has to say so or its 8 MW,
    $47.4M and Q3 2026 keep landing on AZP-2.
    """
    from_filing = blocks.block_key("AZP-3 Phase 3")
    from_article = blocks.block_key("Phase 3", parent="AZP-3")
    assert from_filing.value == from_article.value == "azp-3.phase-3"
    assert not from_filing.generic and not from_article.generic


def test_a_bare_phase_label_is_generic_and_a_named_one_is_not():
    """`generic` is what decides whether a block can be placed at all."""
    assert blocks.block_key("Phase 1").generic
    assert blocks.block_key("Building A").generic
    assert not blocks.block_key("AZP-2").generic
    assert not blocks.block_key("Colossus 2").generic


def test_a_size_is_not_an_identity():
    """ "8 MW expansion" names how much, not which campus.

    Treating it as a designator would let it count as a family in `placeable` and
    license summing two campuses' megawatts together.
    """
    assert blocks.block_key("8 MW expansion").generic
    assert blocks.block_key("initial 8 MW").generic


def test_building_a_keeps_its_letter():
    """`a` is an article and also this block's whole designator."""
    assert blocks.block_key("Building A").value == "building-a"
    assert blocks.block_key("Building B").value == "building-b"
    assert blocks.block_key("Building A").value != blocks.block_key("Building B").value


def test_different_facilities_never_collapse_however_similar():
    """The asymmetry `dedup.py` argues for: a wrong merge is invisible.

    `phase-1` is not `azp-2` on any similarity score. Making them one block is an
    operator's decision, recorded in `block_alias`, never inferred here.
    """
    keys = {
        blocks.block_key(label).value
        for label in ("AZP-2", "AZP-3", "Phase 1", "Colossus 1", "Colossus 2", "VA-1", "VA-2")
    }
    assert len(keys) == 7, "two distinct facilities were folded into one key"


def test_the_key_is_a_pure_function_so_re_ingest_is_idempotent():
    for label, parent in (("AZP-3 Phase 3", None), ("Phase 3", "AZP-3"), ("Building A", "X")):
        assert blocks.block_key(label, parent) == blocks.block_key(label, parent)


def test_an_unreadable_label_still_yields_a_key_rather_than_raising():
    """Called mid-ingest; raising here would drop a whole article."""
    for label in ("", "   ", "???", "—"):
        key = blocks.block_key(label)
        assert key.value
        assert key.generic


def test_label_tokens_cover_every_form_a_quote_might_use():
    """Feeds the per-block evidence check.

    A filing writes "Phase III" where an article writes "third phase", and the
    quote has to be accepted either way or the block loses its citation.
    """
    tokens = blocks.label_tokens("Phase 3", parent="AZP-3")
    assert {"phase", "3", "iii", "third"} <= tokens
    assert "azp" in tokens


# --- the rollup -------------------------------------------------------------


@dataclass
class FakeBlock:
    block_key: str
    mw: float | None = None
    status: str = "planned"
    customer: str | None = None
    parent: str | None = None
    generic: bool = False
    #: Comma-joined, exactly as the column stores it.
    unconfirmed_fields: str | None = None


def test_an_unconfirmed_capacity_is_shown_but_never_summed():
    """The 7,500 MW error, pinned.

    The first backfill tranche raised Applied Digital Jamestown from 7 MW to 7,500
    MW off a single block whose `mw` no quote in the article named. Keeping an
    unconfirmed figure is the point of 待确认; *summing* it launders it into a
    campus total that then reads as cited.
    """
    got = blocks.rollup(
        [
            FakeBlock(
                "hpc-facility", mw=7500.0, status="under_construction", unconfirmed_fields="mw"
            ),
        ]
    )
    assert got.mw_planned is None
    assert got.uncited == ("hpc-facility",)


def test_the_unconfirmed_list_is_read_by_entry_and_not_by_substring():
    """No field today contains "mw" inside a longer name, so this guards a rename.

    Reading the column with `"mw" in raw` behaves identically on the current field
    set and would silently void a cited capacity the moment an `mwh_*` field is
    added. The column is comma-joined, so it costs nothing to split it properly.
    """
    assert blocks.mw_is_confirmed(FakeBlock("phase-1", mw=10.0, unconfirmed_fields="mwh_estimate"))
    assert not blocks.mw_is_confirmed(
        FakeBlock("phase-1", mw=10.0, unconfirmed_fields="customer,mw")
    )


def test_a_block_unconfirmed_in_some_other_field_still_counts_its_capacity():
    """Only `mw` gates the capacity sum. A vague date does not void a cited figure."""
    got = blocks.rollup(
        [FakeBlock("phase-1", mw=48.0, unconfirmed_fields="expected_online,customer")]
    )
    assert got.mw_planned == 48.0
    assert got.uncited == ()


def test_a_confirmed_block_is_not_dragged_down_by_an_unconfirmed_sibling():
    got = blocks.rollup(
        [
            FakeBlock("phase-1", mw=100.0, status="energized"),
            FakeBlock("phase-2", mw=9000.0, unconfirmed_fields="mw"),
        ]
    )
    assert got.mw_planned == 100.0
    assert got.mw_built == 100.0


def test_an_unconfirmed_capacity_does_not_inflate_a_customer_attribution():
    """Capex attributes MW per customer off this, so the same rule has to hold."""
    got = blocks.rollup(
        [
            FakeBlock("phase-1", mw=50.0, customer="Fluidstack"),
            FakeBlock("phase-2", mw=9000.0, customer="Fluidstack", unconfirmed_fields="mw"),
        ]
    )
    assert got.customers == (("Fluidstack", 50.0),)


def test_no_blocks_means_no_opinion():
    """A project with no blocks must be left exactly as it was.

    This is what lets the migration land on 227 rows without changing any of them.
    """
    got = blocks.rollup([])
    assert got.mw_planned is None
    assert got.mw_built is None
    assert got.phase is None
    assert got.customer is None


def test_the_ai_era_case_the_project_row_cannot_express():
    """150 MW serving one buyer, 150 MW building for another, 300 planned.

    The state that started all of this. One `phase` enum cannot say it; the rollup
    reports the campus honestly and the blocks keep the detail.
    """
    got = blocks.rollup(
        [
            FakeBlock("azp-1", mw=150, status="serving", customer="Microsoft"),
            FakeBlock("azp-2", mw=150, status="under_construction", customer="Oracle"),
            FakeBlock("azp-3", mw=300, status="planned"),
        ]
    )
    assert got.mw_planned == 600
    assert got.mw_built == 150, "only the live tranche counts as built"
    assert got.phase == "operational", "something is running, so the campus is"
    assert got.customer == "Microsoft", "the largest block that names one"
    assert got.customers == (("Microsoft", 150.0), ("Oracle", 150.0))


def test_a_pre_lease_is_a_customer_with_nothing_built():
    """49 projects on the live database look like this."""
    got = blocks.rollup([FakeBlock("p1", mw=48, status="planned", customer="Fortune 100")])
    assert got.mw_built is None
    assert got.customer == "Fortune 100"
    assert got.phase == "announced"


def test_a_finished_shell_with_no_power_is_representable():
    """`tracks.py` calls this the most informative signal in the dataset, and
    until now it had nowhere to live."""
    got = blocks.rollup([FakeBlock("p1", mw=100, status="shell_complete")])
    assert got.mw_built is None, "a shell delivers no megawatts"
    assert got.phase == "construction"


def test_a_cancelled_tranche_does_not_cancel_a_live_campus():
    """Today one source saying a phase stopped can flip the whole row."""
    got = blocks.rollup(
        [
            FakeBlock("p1", mw=150, status="serving"),
            FakeBlock("p3", mw=300, status="cancelled"),
        ]
    )
    assert got.phase == "operational"
    assert got.mw_planned == 150, "a cancelled tranche is not planned capacity"


def test_a_wholly_cancelled_campus_is_cancelled():
    got = blocks.rollup([FakeBlock("p1", status="cancelled"), FakeBlock("p2", status="cancelled")])
    assert got.phase == "cancelled"


def test_a_generic_block_is_excluded_when_the_row_holds_two_campuses():
    """The likeliest way this module could corrupt data.

    `dedup_key` is `company|city|state`, so one row holds every facility an
    operator has in one city. An unplaceable "Phase 1" might belong to either, so
    its megawatts are left out rather than guessed at.
    """
    got = blocks.rollup(
        [
            FakeBlock("azp-2", mw=48, status="under_construction"),
            FakeBlock("azp-3", mw=8, status="planned"),
            FakeBlock("phase-1", mw=999, status="planned", generic=True),
        ]
    )
    assert got.mw_planned == 56, "the unplaceable 999 MW must not be summed"
    assert got.unplaceable == ("phase-1",)


def test_a_generic_block_is_kept_when_there_is_only_one_campus():
    """With nothing to confuse it with, "Phase 1" is placeable."""
    got = blocks.rollup(
        [
            FakeBlock("azp-2", mw=48, status="under_construction"),
            FakeBlock("phase-1", mw=10, status="planned", generic=True),
        ]
    )
    assert got.mw_planned == 58
    assert got.unplaceable == ()


def test_a_generic_block_with_a_parent_is_always_placeable():
    got = blocks.rollup(
        [
            FakeBlock("azp-2", mw=48, status="under_construction"),
            FakeBlock("azp-3", mw=8, status="planned"),
            FakeBlock("azp-3.phase-1", mw=4, status="planned", generic=True, parent="AZP-3"),
        ]
    )
    assert got.mw_planned == 60
    assert got.unplaceable == ()


def test_furthest_status_and_the_terminal_override():
    assert blocks.furthest_status(["planned", "energized", "permitting"]) == "energized"
    assert blocks.furthest_status(["planned", "cancelled"]) == "cancelled"
    assert blocks.furthest_status([]) == "planned"
    assert blocks.furthest_status(["", None]) == "planned"


# --- the cache contract -----------------------------------------------------


def test_the_block_cache_is_consistent_after_a_recompute(session):
    """`recompute_blocks` must be a no-op on a database already current.

    Same contract as `recompute_confidence` and `recompute_h200`, and the same
    reason: if running it twice keeps changing rows then either the rebuild is not
    a pure function of what is stored, or `tracker init` is reporting churn that
    is not real.
    """
    from tracker.upsert import recompute_blocks

    assert recompute_blocks(session) == 0
    assert recompute_blocks(session) == 0


def test_a_project_with_no_blocks_is_left_exactly_as_it_was(session):
    """The guarantee that let migration 0009 land on 227 live rows.

    `reconcile` is monotone, and with no blocks it has nothing to say — so every
    scalar, including the ones the "9 of 12" count reads, is untouched.
    """
    from tracker import blocks as blocks_mod
    from tracker.models import Project

    row = Project(
        name="Untouched",
        company="Meta",
        city="New Albany",
        state="OH",
        dedup_key="meta|untouched",
        phase="announced",
        mw_planned=500.0,
    )
    session.add(row)
    session.flush()

    before = (row.mw_planned, row.mw_built, row.phase, row.customer)
    assert blocks_mod.reconcile(row) == []
    assert (row.mw_planned, row.mw_built, row.phase, row.customer) == before


def test_reconcile_never_lowers_a_cited_campus_total(session):
    """A block sum is a floor, not a replacement.

    An article may cite a 1 GW campus while only one 48 MW tranche has been
    described in detail. Replacing 1000 with 48 would silently destroy the larger,
    equally-cited figure — so the larger wins and the smaller is disclosed.
    """
    from tracker import blocks as blocks_mod
    from tracker.models import CapacityBlock, Project

    row = Project(
        name="Big",
        company="Meta",
        city="New Albany",
        state="OH",
        dedup_key="meta|big",
        phase="construction",
        mw_planned=1000.0,
    )
    row.blocks.append(
        CapacityBlock(block_key="azp-2", label="AZP-2", mw=48.0, status="under_construction")
    )
    session.add(row)
    session.flush()

    blocks_mod.reconcile(row)
    assert row.mw_planned == 1000.0, "the cited campus total must survive"


def test_reconcile_raises_a_total_the_blocks_exceed(session):
    from tracker import blocks as blocks_mod
    from tracker.models import CapacityBlock, Project

    row = Project(
        name="Understated",
        company="Meta",
        city="Mesa",
        state="AZ",
        dedup_key="meta|understated",
        phase="construction",
        mw_planned=100.0,
    )
    for key, mw in (("p1", 150.0), ("p2", 150.0)):
        row.blocks.append(CapacityBlock(block_key=key, label=key, mw=mw, status="planned"))
    session.add(row)
    session.flush()

    notes = blocks_mod.reconcile(row)
    assert row.mw_planned == 300.0
    assert any("raised mw_planned" in n for n in notes)


def test_a_customer_already_on_the_row_is_not_churned(session):
    """FILL_ONLY semantics: churn on an existing row is worse than staleness."""
    from tracker import blocks as blocks_mod
    from tracker.models import CapacityBlock, Project

    row = Project(
        name="Held",
        company="Meta",
        city="Mesa",
        state="AZ",
        dedup_key="meta|held",
        phase="construction",
        customer="Original Corp",
    )
    row.blocks.append(
        CapacityBlock(block_key="p1", label="Phase 1", mw=50.0, status="serving", customer="Other")
    )
    session.add(row)
    session.flush()

    blocks_mod.reconcile(row)
    assert row.customer == "Original Corp"


def test_a_cancelled_tranche_listed_first_still_does_not_cancel_the_campus(session):
    """Order must not decide it.

    The same case as above with the blocks the other way round. A rollup that reads
    the first status rather than asking which are still running gets this wrong,
    and which order the rows come back in is not something to depend on.
    """
    got = blocks.rollup(
        [
            FakeBlock("p3", mw=300, status="cancelled"),
            FakeBlock("p1", mw=150, status="serving"),
        ]
    )
    assert got.phase == "operational"


def test_a_wholly_stopped_campus_reports_a_terminal_phase_either_way(session):
    for order in (["paused", "cancelled"], ["cancelled", "paused"]):
        got = blocks.rollup([FakeBlock(f"p{i}", status=s) for i, s in enumerate(order)])
        assert got.phase in {"paused", "cancelled"}
        assert got.mw_planned is None, "a stopped tranche is not planned capacity"


def test_word_order_does_not_fork_one_tranche_into_two():
    """Found on Lake Mariner mid-backfill.

    Two articles wrote "La Lupa (Core42 Leases)" and "Core42 Leases (La Lupa)", and
    the row held the same 60 MW tranche twice under two keys. A block is a set of
    designators, not a phrase, so order cannot be allowed to carry identity.
    """
    a = blocks.block_key("La Lupa (Core42 Leases)")
    b = blocks.block_key("Core42 Leases (La Lupa)")
    assert a.value == b.value


def test_sorting_still_converges_the_parent_case():
    """The AZP-3 convergence must survive the change that fixed Lake Mariner."""
    assert (
        blocks.block_key("AZP-3 Phase 3").value
        == blocks.block_key("Phase 3", parent="AZP-3").value
        == blocks.block_key("Phase 3 of AZP-3").value
    )


def test_a_repeated_word_does_not_change_the_key():
    """Segments are a set, so "Phase 1 Phase 1" is still `phase-1`."""
    assert blocks.block_key("Phase 1 Phase 1").value == blocks.block_key("Phase 1").value
