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

import json
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
    #: What a reader sees. Defaults to the key so the older tests need no changes.
    label: str = ""

    def __post_init__(self) -> None:
        self.label = self.label or self.block_key


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


def test_reconcile_discloses_a_total_the_blocks_exceed_but_keeps_the_cited_one(session):
    """A block sum above the cited campus figure is a QUESTION, not an answer.

    This asserted the opposite until Hyperion (#10) showed what the old rule does
    to a real row: gas plants, a whole-campus restatement filed as "Phase 3", and
    a milestone repeating Phase 1's capacity summed to 14,462 MW, `reconcile`
    wrote that over a cited 5,000, and it did so *after* an audit action had
    already cleared the same bad figure.

    "A block sum is a floor on the campus" holds only if the blocks partition the
    campus. When they do not, the sum is evidence of double-counting — so it is
    disclosed and the cited figure stands.
    """
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
    assert row.mw_planned == 100.0, "the cited figure stands"
    assert any("kept the cited figure" in n for n in notes), notes


def test_reconcile_still_fills_a_null_total(session):
    """The half of the old behaviour that was right, and that keeps 9-of-12 rising."""
    from tracker import blocks as blocks_mod
    from tracker.models import CapacityBlock, Project

    row = Project(
        name="Unsized",
        company="Meta",
        city="Mesa",
        state="AZ",
        dedup_key="meta|unsized",
        phase="construction",
    )
    for key, mw in (("p1", 150.0), ("p2", 150.0)):
        row.blocks.append(CapacityBlock(block_key=key, label=key, mw=mw, status="planned"))
    session.add(row)
    session.flush()

    blocks_mod.reconcile(row)
    assert row.mw_planned == 300.0


def test_reconcile_discloses_a_built_figure_the_tranches_exceed(session):
    """`mw_built` carried the other half of the Hyperion mistake, found second.

    The fix was applied to `mw_planned` and the next clause went on raising
    `mw_built` to the tranche sum three lines later — and a whole-campus restatement
    filed as a sibling tranche is `serving` as readily as it is `planned`, so the
    partition argument fails identically. It also undid the merge: `resolve` had just
    lowered `mw_built` to what the claims support, and this raised it straight back.
    """
    from tracker import blocks as blocks_mod
    from tracker.models import CapacityBlock, Project

    row = Project(
        name="Restated",
        company="Oracle",
        city="Abilene",
        state="TX",
        dedup_key="oracle|restated",
        phase="operational",
        mw_built=200.0,
    )
    for key, mw in (("phase-1", 600.0), ("whole-campus", 600.0)):
        row.blocks.append(CapacityBlock(block_key=key, label=key, mw=mw, status="serving"))
    session.add(row)
    session.flush()

    notes = blocks_mod.reconcile(row)
    assert row.mw_built == 200.0, "the cited built figure stands"
    assert any("kept the cited figure" in n for n in notes), notes


def test_reconcile_still_fills_a_null_built_figure(session):
    """The half of the old behaviour that was right, kept."""
    from tracker import blocks as blocks_mod
    from tracker.models import CapacityBlock, Project

    row = Project(
        name="Unbuilt",
        company="Oracle",
        city="Abilene",
        state="TX",
        dedup_key="oracle|unbuilt",
        phase="operational",
    )
    for key, mw in (("p1", 150.0), ("p2", 150.0)):
        row.blocks.append(CapacityBlock(block_key=key, label=key, mw=mw, status="serving"))
    session.add(row)
    session.flush()

    blocks_mod.reconcile(row)
    assert row.mw_built == 300.0


def test_generation_is_not_the_campus(session):
    """A utility's gas plant is real, cited, placeable — and not a data center.

    Hyperion carried 5,962 MW of Entergy's plant as tranches of the campus. Two
    different quantities were added together: a plant's nameplate output and a
    data center's IT load. Generation to serve a site is normally LARGER than the
    load it serves, so this inflates rather than merely blurring.
    """
    from tracker import blocks as blocks_mod
    from tracker.models import CapacityBlock, Project

    row = Project(
        name="With plant",
        company="Meta",
        city="Rayville",
        state="LA",
        dedup_key="meta|withplant",
        phase="construction",
    )
    for key, label, mw in (
        ("hall-1", "Phase 1", 500.0),
        ("gas", "Franklin Farms Gas Plants", 2262.0),
        ("solar", "Franklin Farms Solar", 1500.0),
        ("sub", "Smalling Substation", 55.0),
    ):
        row.blocks.append(CapacityBlock(block_key=key, label=label, mw=mw, status="planned"))
    session.add(row)
    session.flush()

    got = blocks_mod.rollup(list(row.blocks))
    assert got.mw_planned == 500.0, "only the data hall counts"
    assert set(got.generation) == {"gas", "solar", "sub"}

    # Named on screen, not silently dropped: the module's own discipline is that
    # nothing leaves the arithmetic without a line saying where it went.
    notes = blocks_mod.reconcile_notes(got)
    assert any("generation or grid plant" in n for n in notes), notes

    accounting = blocks_mod.account(row)
    assert any(r.reason == "generation" for r in accounting.residuals)


def test_a_utility_energising_its_own_plant_is_not_the_campus_going_live(session):
    """One of the ways Hyperion came to read `operational` with nothing built."""
    from tracker import blocks as blocks_mod
    from tracker.models import CapacityBlock, Project

    row = Project(
        name="Plant live",
        company="Meta",
        city="Rayville",
        state="LA",
        dedup_key="meta|plantlive",
        phase="construction",
    )
    row.blocks.append(
        CapacityBlock(block_key="hall-1", label="Phase 1", mw=500.0, status="under_construction")
    )
    row.blocks.append(
        CapacityBlock(block_key="gas", label="Gas Turbine 1", mw=800.0, status="serving")
    )
    session.add(row)
    session.flush()

    got = blocks_mod.rollup(list(row.blocks))
    assert got.phase == "construction", "the serving gas turbine must not promote the campus"


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


# --- accounting: the parts must sum to the whole ------------------------------
#
# Measured on the live database: 70 of 118 itemised projects showed tranches summing
# to *less* than the campus total, because untrustworthy capacity was quietly left
# out. Each reads on screen as 250 + 150 != 750, and what a reader concludes is not
# "one figure is unconfirmed" but "these people cannot add up".


@dataclass
class FakeProject:
    mw_planned: float | None = None
    blocks: list = None  # type: ignore[assignment]
    sources: list = None  # type: ignore[assignment]

    def __post_init__(self):
        self.blocks = self.blocks or []
        self.sources = self.sources or []


def test_an_unconfirmed_tranche_is_accounted_for_rather_than_dropped():
    """Polaris Forge 1 exactly: 100 + 150 counted, 150 待确认, 400 cited."""
    got = blocks.account(
        FakeProject(
            mw_planned=400.0,
            blocks=[
                FakeBlock("eln-02", mw=100.0, status="serving"),
                FakeBlock("eln-03", mw=150.0, status="under_construction"),
                FakeBlock("building-4", mw=150.0, unconfirmed_fields="mw"),
            ],
        )
    )
    assert got.counted_mw == 250.0
    assert got.closes, "the lines must add up to the cited total"
    assert [(r.reason, r.mw) for r in got.residuals] == [("unconfirmed", 150.0)]


def test_a_campus_only_partly_itemised_names_the_remainder():
    """The 70-project case. The gap gets a line instead of being left on screen."""
    got = blocks.account(
        FakeProject(mw_planned=750.0, blocks=[FakeBlock("phase-1", mw=250.0, status="serving")])
    )
    assert got.closes
    assert [(r.reason, r.mw) for r in got.residuals] == [("unitemised", 500.0)]


def test_tranches_that_exceed_the_cited_total_are_named_not_hidden():
    """The other direction: a stale campus figure, or one building counted twice.

    Two sibling keys on purpose, not a nested pair — the duplicate that actually
    occurs is `building-2.eln-2` beside `building-2.forge-1.polaris`, two articles
    naming one building different ways. Neither key contains the other, which is why
    it went undetected and why the overlap line has to exist.
    """
    got = blocks.account(
        FakeProject(
            mw_planned=100.0,
            blocks=[
                FakeBlock("building-2.eln-2", mw=100.0),
                FakeBlock("building-2.forge-1.polaris", mw=100.0),
            ],
        )
    )
    assert got.closes
    assert [(r.reason, r.mw) for r in got.residuals] == [("overlap", 100.0)]


def test_with_no_cited_total_the_sum_of_parts_is_shown_as_a_floor():
    got = blocks.account(
        FakeProject(mw_planned=None, blocks=[FakeBlock("phase-1", mw=60.0, status="serving")])
    )
    assert got.total == 60.0
    assert got.total_is_floor
    assert got.closes


def test_a_cancelled_tranche_is_outside_the_campus_total():
    got = blocks.account(
        FakeProject(
            mw_planned=100.0,
            blocks=[
                FakeBlock("phase-1", mw=100.0, status="serving"),
                FakeBlock("phase-2", mw=500.0, status="cancelled"),
            ],
        )
    )
    assert got.counted_mw == 100.0
    assert got.closes and got.residuals == ()


def test_naming_a_tranche_without_sizing_it_is_not_a_discrepancy():
    """15 live projects do this — "VA2 is under construction", no megawatts published."""
    got = blocks.account(FakeProject(mw_planned=None, blocks=[FakeBlock("va-2")]))
    assert got.total is None
    assert got.closes, "inventing a fault to go with a number nobody stated"


def test_a_project_with_no_blocks_accounts_for_nothing():
    got = blocks.account(FakeProject(mw_planned=100.0))
    assert got.counted_mw == 0.0
    assert [r.reason for r in got.residuals] == ["unitemised"]


# --- itemisation: a bare row must say why it is bare --------------------------


@dataclass
class FakeSource:
    extractor: str | None = "crawl:v1"
    blocks: str | None = None


def test_a_read_that_found_nothing_is_distinguishable_from_never_reading():
    """The convergence bug. Both looked like NULL, so 40% of the spend was re-reads."""
    unread = FakeProject(sources=[FakeSource(blocks=None)])
    read = FakeProject(sources=[FakeSource(blocks="[]")])
    assert blocks.itemisation(unread) == blocks.UNREAD
    assert blocks.itemisation(read) == blocks.SINGLE_BLOCK


def test_one_unread_article_among_several_keeps_the_project_unread():
    project = FakeProject(sources=[FakeSource(blocks="[]"), FakeSource(blocks=None)])
    assert blocks.itemisation(project) == blocks.UNREAD


def test_a_project_with_no_article_is_not_reported_as_unread():
    """Census lookups and queue rows have no prose behind them to read."""
    project = FakeProject(sources=[FakeSource(extractor="derived:census-place")])
    assert blocks.itemisation(project) == blocks.NO_ARTICLE


def test_having_blocks_beats_every_other_state():
    project = FakeProject(blocks=[FakeBlock("phase-1", mw=1.0)], sources=[FakeSource()])
    assert blocks.itemisation(project) == blocks.ITEMISED


def test_every_itemisation_state_has_a_sentence_for_the_reader():
    for state in (blocks.ITEMISED, blocks.SINGLE_BLOCK, blocks.UNREAD, blocks.NO_ARTICLE):
        assert blocks.ITEMISATION_NOTES[state]


def test_every_residual_reason_has_a_sentence_for_the_reader():
    """A residual with no explanation is the mismatch again, one line lower."""
    for reason in blocks.RESIDUAL_REASONS:
        assert blocks.Residual(reason, 1.0).note


def test_a_figure_larger_than_its_own_campus_is_rejected_not_absorbed():
    """Portland 3: a "Hillsboro Phase 1" of 36,000 MW against a cited 144 MW campus.

    Left in the arithmetic it was swallowed by the overlap line as "counted twice over
    -35,988 MW", a sentence that means nothing — nothing was counted twice, one figure
    is wrong by three orders of magnitude. It is disclosed on its own line and kept
    out of every sum, so the panel's headline and its total cannot disagree.
    """
    got = blocks.account(
        FakeProject(
            mw_planned=144.0,
            blocks=[
                FakeBlock(
                    "hillsboro.phase-1", mw=36000.0, status="serving", unconfirmed_fields="mw"
                ),
                FakeBlock("total", mw=126.0, unconfirmed_fields="mw"),
            ],
        )
    )
    reasons = {r.reason: r.mw for r in got.residuals}
    assert reasons["out_of_scale"] == 36000.0
    assert reasons["unconfirmed"] == 126.0
    assert "overlap" not in reasons, "a bad reading must not masquerade as double counting"
    assert got.closes, "the rejected figure is disclosed, never summed"


def test_a_plausible_tranche_larger_than_the_total_is_still_only_an_overlap():
    """The threshold has to leave the real case alone: a stale campus figure."""
    got = blocks.account(FakeProject(mw_planned=100.0, blocks=[FakeBlock("phase-1", mw=150.0)]))
    reasons = {r.reason for r in got.residuals}
    assert reasons == {"overlap"}


def test_with_no_cited_campus_figure_nothing_can_be_called_out_of_scale():
    """There is nothing to be out of scale *with*, so the sum of parts stands."""
    got = blocks.account(FakeProject(mw_planned=None, blocks=[FakeBlock("phase-1", mw=36000.0)]))
    assert got.total == 36000.0
    assert not [r for r in got.residuals if r.reason == "out_of_scale"]


# --- placeholders -----------------------------------------------------------


PLACEHOLDER_URL = "https://news.microsoft.com/PLACEHOLDER-replace-with-the-release/"


@dataclass
class FakeCitation:
    """Only what `blocks_by_key` reads off a `Source` row.

    Distinct from `FakeSource` above, which stands in for `itemisation`'s much
    narrower view of the same table.
    """

    id: int
    url: str
    source_type: str
    blocks: str
    fetched_at: str = "2026-01-01"


def _one_block(sources):
    (block,) = blocks.blocks_by_key(sources).values()
    return block


def test_a_placeholder_citation_cannot_name_or_size_a_block():
    """The block-level half of the Fairwater (#1) placeholder defect.

    `sample-projects.json` types an unreplaced seed URL `company_filing` — weight 3
    against `trade_press`'s 2 — so left alone it wins `mw` on weight *and* takes
    `label`/`parent`, which are handed to whichever source is seen first rather
    than resolved by weight at all.
    """
    block = _one_block(
        [
            FakeCitation(
                1,
                PLACEHOLDER_URL,
                "company_filing",
                json.dumps([{"label": "Phase 1", "mw": 999.0, "status": "serving"}]),
            ),
            FakeCitation(
                2,
                "https://www.dcd.com/a",
                "trade_press",
                json.dumps([{"label": "Phase 1", "mw": 200.0, "status": "under_construction"}]),
            ),
        ]
    )
    assert block["mw"] == 200.0, "weight 3 on a URL that does not exist must not win"
    assert block["status"] == "under_construction", (
        "the ladder ignores weight, so only the 待确认 flag stops 'serving' here"
    )


def test_a_placeholder_alone_still_produces_a_block():
    """Demoted, not dropped — `--allow-placeholders` still has to smoke-test blocks."""
    block = _one_block(
        [
            FakeCitation(
                1,
                PLACEHOLDER_URL,
                "company_filing",
                json.dumps([{"label": "Phase 1", "mw": 100.0, "status": "planned"}]),
            )
        ]
    )
    assert block["label"] == "Phase 1"
    assert block["mw"] == 100.0
