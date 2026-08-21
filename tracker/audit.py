"""Find figures that are physically or economically implausible.

`logic check` asks whether a row's fields contradict *each other*. This asks a
different question: whether a number could be true at all. The two failures it
hunts have both happened on the live database, and neither is a contradiction —
each row was perfectly self-consistent around a figure that was wrong by three
orders of magnitude:

* Portland 3 came back with two "Hillsboro" tranches at 36,000 MW against a cited
  144 MW campus — kilowatts read as megawatts.
* **Project 72**, the one this command exists for: Flexential's Englewood
  *expansion* at 11,250 MW. That is a 待确认 figure with no quote behind it, on a
  colocation operator whose whole portfolio is under 500 MW, implying $187k per MW
  against the $8-12M a real build costs. Three independent smells — and it sat as
  the largest number in the database with nothing to challenge it, feeding an
  8.7-million-H200 estimate and every national total.

Everything here is free: no LLM, no network, read-only. Each check states the
physics or the economics it leans on, because a threshold nobody can defend is a
threshold somebody will delete.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import Project

log = logging.getLogger(__name__)

#: Dollars per megawatt outside which one of the two figures is wrong. The band is
#: deliberately generous: a bare powered shell can be under $2M/MW and a
#: liquid-cooled AI build with land runs past $30M/MW, so anything outside
#: [$0.3M, $60M] is not an expensive or a cheap project — it is a unit error.
USD_PER_MW_FLOOR = 300_000
USD_PER_MW_CEILING = 60_000_000

#: Above this, a single campus exceeds the largest genuinely-planned campuses in
#: the world (the multi-gigawatt Stargate sites). It is possible the industry will
#: outgrow this; it is much more likely the figure is kilowatts or square feet.
SINGLE_CAMPUS_CEILING_MW = 8_000

#: A claimed capacity at least this many times a sibling claim for the same field
#: is read as the same figure in different units, not a disagreement. 1000x is
#: kW-as-MW; 100x catches a misplaced decimal against a rounded sibling.
UNIT_RATIOS = (1000.0, 100.0)
UNIT_RATIO_TOLERANCE = 0.15

#: A claim this many times the lowest one is what "the low figure is right" rules
#: against. Derived from the check's own ratios rather than picked, so an action
#: cannot rule against a claim the check would not have called a unit misread — a
#: merely-disagreeing 200 MW beside 144 MW is a job for `tracker logic conflicts`.
_UNIT_SUSPECT_FLOOR: Final = min(UNIT_RATIOS) * (1 - UNIT_RATIO_TOLERANCE)

#: `mw_planned` above this on a row whose figure is 待确认 (no quote anywhere
#: names it) is flagged. A gigawatt claim resting on nothing quotable deserves a
#: human eye before it deserves a place in a total.
GIANT_UNCONFIRMED_MW = 1_000

#: Below this, a campus's stated capex cannot pay for its stated capacity.
#:
#: A gigawatt-scale AI campus runs $8-15M per MW for buildings and power
#: infrastructure alone. This sits at a third of the low end deliberately: it
#: should fire on "superseded by five times" and stay silent on a merely cheap
#: build. Hyperion's $10B over 5,000 MW is $2M/MW.
_BUILD_COST_FLOOR_USD_PER_MW: Final = 3_000_000

#: Only asked of campuses big enough for the industry figures to apply. A 20 MW
#: colocation shell genuinely can cost less per MW than a liquid-cooled AI hall.
_SUPERSEDED_MIN_MW: Final = 500


@dataclass(frozen=True)
class UnitFinding:
    project_id: int
    name: str
    code: str
    summary: str
    #: What to do about it — every finding must say, or it is just an alarm.
    remedy: str


def _claim_rows(project: Project, field: str, *, live_only: bool = True) -> list[tuple[Any, float]]:
    """(source, value) for every positive numeric claim about one field.

    `live_only` drops claims a decision has already ruled against, which is what
    stops a repaired row being reported forever as a disagreement between a figure
    and the figure that replaced it. `audit check` does not filter by
    `settled_codes`, so without this the finding count could never fall. The one
    reader that wants them all is `evidence_block`, which labels them.
    """
    from tracker.upsert import _decided_against

    out: list[tuple[Any, float]] = []
    for source in project.sources:
        if not source.claims:
            continue
        try:
            value = json.loads(source.claims).get(field)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            continue
        if live_only and field in _decided_against(source):
            continue
        out.append((source, float(value)))
    return out


def _claims_for(project: Project, field: str) -> list[float]:
    """Every numeric value any source still claims for one field."""
    return [value for _, value in _claim_rows(project, field)]


def _mw_unconfirmed(project: Project) -> bool:
    """True when no source's quote confirms the winning capacity."""
    from tracker.gaps import basis

    return basis(project, "mw_planned") == "unconfirmed"


def check_project(project: Project, *, settings: Any = None) -> list[UnitFinding]:
    """Every plausibility check against one row. Pure, read-only."""
    from tracker import blocks as blocks_mod
    from tracker.compute import h200_equivalent

    out: list[UnitFinding] = []

    def add(code: str, summary: str, remedy: str) -> None:
        out.append(UnitFinding(project.id, project.name, code, summary, remedy))

    mw = project.mw_planned

    # --- the same figure in two different units ------------------------------
    claims = sorted(_claims_for(project, "mw_planned"))
    if len(claims) >= 2:
        low, high = claims[0], claims[-1]
        for ratio in UNIT_RATIOS:
            if abs(high / low - ratio) <= ratio * UNIT_RATIO_TOLERANCE:
                add(
                    "same_figure_two_units",
                    f"sources claim {low:g} and {high:g} MW — a factor of ~{ratio:g}, "
                    "which is a unit misread, not a disagreement",
                    f"the small figure is almost always right ({high:g} is likely "
                    f"kW or a decimal slip); re-read the article claiming {high:g}",
                )
                break

    # --- a campus bigger than the biggest campus on earth --------------------
    if mw and mw > SINGLE_CAMPUS_CEILING_MW:
        add(
            "campus_exceeds_worlds_largest",
            f"{mw:g} MW planned on one campus — beyond any site actually planned "
            "anywhere; almost always kilowatts read as megawatts",
            "re-read the source; if the figure survives, it is news worth checking by hand anyway",
        )

    # --- a gigawatt resting on no quote ---------------------------------------
    if mw and mw >= GIANT_UNCONFIRMED_MW and _mw_unconfirmed(project):
        add(
            "giant_capacity_unconfirmed",
            f"{mw:g} MW planned, and no quote in any article names that figure "
            "(待确认) — a gigawatt claim resting on nothing quotable",
            f"tracker backfill blocks --project {project.id} --force, or read the source by hand",
        )

    # --- dollars per megawatt outside physics ---------------------------------
    if mw and mw >= 5 and project.investment_usd:
        per_mw = project.investment_usd / mw
        if per_mw < USD_PER_MW_FLOOR or per_mw > USD_PER_MW_CEILING:
            direction = "low" if per_mw < USD_PER_MW_FLOOR else "high"
            add(
                "usd_per_mw_out_of_band",
                f"${per_mw:,.0f} per MW (${project.investment_usd:,} over {mw:g} MW) "
                f"— implausibly {direction}; real builds run $2M-$30M per MW, so one "
                "of the two figures is wrong",
                "check which figure has the quote; the unquoted one is the suspect",
            )

    # --- capex far below what the capacity costs to build ---------------------
    #
    # A gigawatt-scale AI campus costs $8-15M per MW for the buildings and power
    # infrastructure alone (`prompts/_industry.txt` §4). A multi-GW site reported
    # at a fifth of that is nearly always a SUPERSEDED figure — these projects are
    # announced small and enlarged repeatedly, and the first number keeps
    # circulating.
    #
    # `usd_per_mw_out_of_band` does not catch it: Hyperion's $10B over 5,000 MW is
    # $2M/MW, comfortably inside that check's deliberately generous [$300k, $60M]
    # band. The band is wide because it hunts unit misreads; this is a different
    # question, so it gets its own check rather than a narrower band that would
    # start firing on real small projects.
    if project.investment_usd and mw and mw >= _SUPERSEDED_MIN_MW:
        per_mw = project.investment_usd / mw
        if per_mw < _BUILD_COST_FLOOR_USD_PER_MW:
            add(
                "investment_below_build_cost",
                f"${per_mw / 1e6:.1f}M per MW (${project.investment_usd:,} over {mw:g} MW) "
                f"— a campus this size costs $8-15M per MW to build, so this figure is "
                "probably an early announcement the project has since outgrown",
                "rule the early claim out (`o`) and the later figure wins the merge on "
                "its own — the claim is superseded, never the field edited",
            )

    # --- the H200 estimate disagreeing with its own input ---------------------
    # `h200_equivalent` is derived from capacity, so the pair can only disagree
    # when one moved and the other did not — which is how Applied Digital
    # Jamestown was caught saying 7,500 MW beside an H200 figure meaning 7 MW.
    if project.h200_equivalent and mw:
        expected = h200_equivalent(project.mw_built or mw, settings=settings)
        if expected and not (0.5 <= project.h200_equivalent / expected <= 2.0):
            add(
                "h200_disagrees_with_capacity",
                f"h200_equivalent {project.h200_equivalent:,} implies a different "
                f"capacity than the {mw:g} MW stored — the two moved independently",
                "tracker init recomputes it; if it still disagrees, the capacity "
                "changed recently and is the thing to check",
            )

    # --- tranches out of scale with their own campus ---------------------------
    if project.blocks:
        got = blocks_mod.account(project)
        rejected = [r for r in got.residuals if r.reason == "out_of_scale"]
        for residual in rejected:
            add(
                "block_out_of_scale",
                f"tranche(s) {', '.join(residual.labels)} total {residual.mw:g} MW "
                f"against a {got.total:g} MW campus — larger than the whole site",
                "already excluded from every total; fix by re-reading the source "
                f"(tracker backfill blocks --project {project.id} --force)",
            )

    return out


def run(session: Session, *, project_ids: list[int] | None = None) -> list[UnitFinding]:
    """Audit every project, or the given ones. Findings, worst-first."""
    query = select(Project)
    if project_ids:
        query = query.where(Project.id.in_(project_ids))
    findings: list[UnitFinding] = []
    for project in session.scalars(query).all():
        findings.extend(check_project(project))
    #: Unit misreads first — they are the ones that poison totals.
    order = {
        "same_figure_two_units": 0,
        "campus_exceeds_worlds_largest": 1,
        "block_out_of_scale": 2,
        "giant_capacity_unconfirmed": 3,
        "usd_per_mw_out_of_band": 4,
        "investment_below_build_cost": 5,
        "h200_disagrees_with_capacity": 6,
    }
    findings.sort(key=lambda f: (order.get(f.code, 9), f.project_id))
    return findings


# --- Settling one -------------------------------------------------------------
#
# Finding an impossible number was only ever half the job. `tracker audit` has
# reported the same 11,250 MW colocation expansion on every run since it was
# written, because the only repair it could offer was a sentence telling somebody
# to go and read an article. A report nobody can answer is a report nobody reads.
#
# So each finding declares the concrete edits that answer it, and `resolve` walks a
# ladder from free to expensive:
#
#   1. **Arithmetic**, where the answer is not a judgement at all. Free.
#   2. **The person at the keyboard**, with the claims and their quotes on screen
#      and single-key answers. Free, and better than anything below it.
#   3. **A reasoning model**, given every claim, quote and source on the row.
#   4. **The open web**, when the model says the row does not contain the answer —
#      search, fetch, and put the sentences that mention the figure in front of it.
#   5. **The model again**, with what the search found.
#
# The ladder only ever descends when the rung above declined. That ordering is the
# whole cost control: stage 1 costs nothing, stage 2 costs nothing, and most rows
# never reach stage 4.


@dataclass(frozen=True)
class Action:
    """One edit that answers one finding. `apply` returns what it changed."""

    key: str
    label: str
    apply: Any  # (session, project, finding) -> str


def _fmt(value: Any) -> str:
    """A value as a decision sentence writes it.

    `None` renders `empty`, which is the fix for four `TypeError`s: `{was:g}` on a
    column an earlier action on the same project had already emptied. `cli` builds
    the whole pending list before applying anything, so two findings on one row
    reached the second formatter with a None in hand.
    """
    if value is None:
        return "empty"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _rule_against(
    session: Session, project: Project, field: str, sources: list[Any], why: str
) -> str:
    """Mark these citations' claims about one field superseded, then re-derive.

    **This is the whole repair, and the reason every action here changed shape.** A
    project scalar is a cache: `upsert.recompute_from_sources` re-derives it from the
    claim set on the next ingest, merge or `backfill derive`, so an action that
    assigned the column was undone silently and the finding came back. `audit`'s own
    remedy for `investment_below_build_cost` has said so all along — "mark the old
    claim `superseded` rather than editing the field".

    The field is emptied *before* the recompute rather than assigned after it.
    `upsert.resolve` returns the existing value when no claim participates, so a
    field whose every claim has been ruled out stays empty — durably, and without
    this function ever choosing a number. Where a claim survives, the recompute picks
    it and the sentence reports what it picked, which is what `settled_codes` then
    checks the row against.
    """
    from tracker.conflicts import supersede
    from tracker.upsert import recompute_from_sources

    was = getattr(project, field)
    marked = sum(1 for source in sources if supersede(source, field))
    setattr(project, field, None)
    session.flush()
    recompute_from_sources(session, project)
    now = getattr(project, field)
    tail = f"{marked} claim(s) superseded" if marked else "nothing left to rule against"
    return f"{field} {_fmt(was)} -> {_fmt(now)} ({why}; {tail})"


def _lowest_claim(session: Session, project: Project, _f: UnitFinding) -> str:
    """Adopt the smallest cited capacity — the classic kilowatts-as-megawatts fix."""
    rows = _claim_rows(project, "mw_planned")
    low = min((value for _, value in rows), default=None)
    if low is None:
        return _rule_against(session, project, "mw_planned", [], "no claim left to choose between")
    losers = [s for s, value in rows if value > low * _UNIT_SUSPECT_FLOOR]
    return _rule_against(session, project, "mw_planned", losers, "the lower cited figure stands")


def _kilowatt_capacity(session: Session, project: Project, _f: UnitFinding) -> str:
    """Rule out every campus figure beyond the world's largest — it is kilowatts.

    This used to divide the stored figure by 1000, which was epistemically sound and
    *durably impossible*: the claim still said 36,000 and every recompute re-read it.
    Nothing states the corrected 36, so there is nowhere for it to live — inventing a
    citation for it is the one thing the evidence model refuses. So the claim is ruled
    out, the field empties, and the row becomes a work item `gaps` already routes back
    into enrichment. A null is answerable; a hand-divided figure looks cited and is not.
    """
    losers = [
        s for s, value in _claim_rows(project, "mw_planned") if value > SINGLE_CAMPUS_CEILING_MW
    ]
    return _rule_against(
        session,
        project,
        "mw_planned",
        losers,
        f"read as kilowatts; re-read with `tracker backfill blocks --project {project.id} --force`",
    )


def _clear_capacity(session: Session, project: Project, _f: UnitFinding) -> str:
    losers = [s for s, _ in _claim_rows(project, "mw_planned")]
    return _rule_against(session, project, "mw_planned", losers, "no claim is trustworthy")


def _clear_investment(session: Session, project: Project, _f: UnitFinding) -> str:
    losers = [s for s, _ in _claim_rows(project, "investment_usd")]
    return _rule_against(session, project, "investment_usd", losers, "no claim is trustworthy")


def _outgrown_investment(session: Session, project: Project, _f: UnitFinding) -> str:
    """Rule out capex figures too small to have built the stored capacity.

    Re-derived the way `check_project` found them rather than carried on the finding,
    for the reason `_out_of_scale_blocks` gives. A later, larger figure on the row
    then wins the recompute on its own.
    """
    mw = project.mw_planned or 0.0
    losers = [
        s
        for s, value in _claim_rows(project, "investment_usd")
        if mw and value / mw < _BUILD_COST_FLOOR_USD_PER_MW
    ]
    return _rule_against(
        session,
        project,
        "investment_usd",
        losers,
        "an early announcement the project has outgrown",
    )


def _recompute_h200(session: Session, project: Project, _f: UnitFinding) -> str:
    """Arithmetic, not a judgement. Nothing is ruled against.

    Routed through the recompute rather than assigning the column, so it honours a
    cited chip count the way `upsert.apply_h200_equivalent` does — the old helper
    overwrote one with a figure derived from capacity.
    """
    from tracker.upsert import recompute_from_sources

    was = project.h200_equivalent
    session.flush()
    recompute_from_sources(session, project)
    return (
        f"h200_equivalent {_fmt(was)} -> {_fmt(project.h200_equivalent)} (re-derived from capacity)"
    )


def _out_of_scale_blocks(project: Project) -> list:
    """The tranches the campus cannot contain, recomputed the way the check found them.

    Re-derived rather than carried on the finding: `check_project` is pure and its
    findings are plain values, and a list of ORM rows on a dataclass that outlives
    its session is the bug that `DuplicatePair` documents. Same call, same answer.
    """
    from tracker import blocks as blocks_mod

    got = blocks_mod.account(project)
    labels = {
        label
        for residual in got.residuals
        if residual.reason == "out_of_scale"
        for label in residual.labels
    }
    return [b for b in (getattr(project, "blocks", ()) or ()) if b.label in labels]


def _blocks_not_counted(session: Session, project: Project, _f: UnitFinding) -> str:
    """Stop counting tranches the campus cannot contain. The figure stays visible.

    Superseding cannot express this: `mw` is not a `WRITABLE_FIELD`, so
    `conflicts.supersede` would drop it from `unconfirmed_fields` silently. Blocks
    have their own 待确认 tier one level down, in the `source.blocks` JSON, and
    `mw_is_confirmed` reads it to show a tranche's capacity while keeping it out of
    every sum — which is exactly "this tranche's number is wrong".

    It matters more than it looks: `rollup` does *not* apply `account`'s out-of-scale
    filter, so a confirmed tranche larger than its own campus still feeds `reconcile`
    and could raise a campus scalar. Marking it stops that.

    This replaces two actions that divided the figure or deleted it. Both lived in the
    same JSON and so had the same durability — a re-crawl overwrites `source.blocks`
    wholesale — and both were worse: one rewrote what the extraction found, the other
    threw away a number a person could check.
    """
    from tracker import blocks as blocks_mod

    blocks = _out_of_scale_blocks(project)
    labels = sorted(b.label for b in blocks)
    if not labels:
        return "tranches not counted: none — nothing is out of scale any more"
    touched = blocks_mod.mark_mw_unconfirmed(session, project, {b.block_key for b in blocks})
    listed = ", ".join(repr(label) for label in labels)
    return (
        f"tranches not counted: {listed} (larger than the whole campus; capacity marked "
        f"待确认 on {touched} citation(s) — re-read with `tracker backfill blocks "
        f"--project {project.id} --force`)"
    )


def _dismiss(session: Session, project: Project, _f: UnitFinding) -> str:
    """Write nothing. The record of the decision is the point."""
    return "left as it stands — the figure was judged correct"


#: finding code -> the edits that answer it. `d` (dismiss) is offered on every
#: finding and is a real answer: an implausible-looking figure that a source
#: actually states is a fact about an unusual project, and recording that stops the
#: check asking again.
#:
#: **No entry writes a project scalar, and none rewrites a value to something nobody
#: claimed.** Every one of them rules a *claim* out and lets the merge engine
#: re-derive the field, because a scalar is a cache: assigning it produced a repair
#: the next recompute silently undid, which is how the same 11,250 MW expansion
#: survived every run of this command.
#:
#: `_divide_by_1000` used to sit here, on the reasoning that a unit misread means the
#: source said 36,000 and meant 36,000 kW, so 36 MW is that source's own figure in the
#: column's units. That reasoning is sound and it does not survive contact with the
#: claim table: the claim still says 36,000, so the corrected value has nowhere to
#: live, and inventing a citation for it is the one thing the evidence model refuses.
#: Ruling the claim out and leaving the field empty is what can actually be recorded.
ACTIONS: Final[dict[str, tuple[Action, ...]]] = {
    "same_figure_two_units": (
        Action("l", "the low figure is right — rule out the kW/decimal-slip claim", _lowest_claim),
        Action("c", "neither is trustworthy — rule out both", _clear_capacity),
    ),
    "campus_exceeds_worlds_largest": (
        Action(
            "k", "it is kilowatts — rule out the claim and re-read the source", _kilowatt_capacity
        ),
        Action("c", "rule out the capacity until a source is re-read", _clear_capacity),
    ),
    "giant_capacity_unconfirmed": (
        Action("c", "no quote names it — rule out the claim", _clear_capacity),
    ),
    "usd_per_mw_out_of_band": (
        Action("i", "the investment figure is the wrong one — rule it out", _clear_investment),
        Action("c", "the capacity is the wrong one — rule it out", _clear_capacity),
    ),
    # This offered nothing at all, while its own remedy text named the right
    # mechanism. Now it uses it.
    "investment_below_build_cost": (
        Action("o", "an early figure the project outgrew — rule it out", _outgrown_investment),
        Action("i", "no capex figure is trustworthy — rule them all out", _clear_investment),
    ),
    "h200_disagrees_with_capacity": (
        Action("r", "re-derive the H200 estimate from the stored capacity", _recompute_h200),
    ),
    # This used to offer nothing, on the reasoning that the tranche is already
    # excluded from every total so the repair is a re-crawl. That was wrong twice
    # over: `rollup` ignores that exclusion and still feeds `reconcile`, and five of
    # the twenty-two findings on the live database are one tranche labelled "2.4 MW
    # Lease" carrying 2400 on a 15 MW campus. What can be recorded durably is that the
    # figure is not countable — not a corrected number for it.
    "block_out_of_scale": (
        Action("u", "the tranche figure cannot be counted — mark it 待确认", _blocks_not_counted),
    ),
}

#: Offered on every finding, after the code's own options.
DISMISS = Action("d", "the figure is right as it stands — stop asking", _dismiss)


def actions_for(code: str) -> tuple[Action, ...]:
    return (*ACTIONS.get(code, ()), DISMISS)


#: One recorded decision: the code, and the sentence saying what it did.
#:
#: The character class is wider than any code in use. It costs nothing now and a
#: code containing a digit would otherwise never match — silently, and only in the
#: skip path, which is the hardest place to notice a regex that stopped working.
_DECISION = re.compile(r"resolved `([a-z0-9_-]+)`: *([^\n]*)")

#: An edit of one project scalar, in the shape every value-changing action writes:
#: `mw_planned 13620 -> empty (no source states it)`, `investment_usd 10,000 ->
#: empty`, `h200_equivalent 1,000 -> 2,000 (recomputed from capacity)`. A decision
#: that is not an edit — a dismissal, `removed 2 milestone(s)`, `closed 3
#: obstacle(s)` — deliberately does not match.
_EDIT = re.compile(r"^([a-z][a-z0-9_]*) .+? -> ([^(—]+?)(?: *[(—]|$)")


#: A block-level decision, which `_EDIT` deliberately cannot match — it has no `->`
#: and names no Project column. It needs checking anyway, and for a sharper reason
#: than a scalar does: `source.blocks` has no `DECIDED_REASONS` carry, so a re-crawl
#: or a `backfill blocks --force` restores the confirmed figure and the question is
#: genuinely open again. Left unchecked, these codes were settled permanently — the
#: same muzzling this function exists to prevent, in the one place it still happened.
_BLOCK_EDIT = re.compile(r"^tranches not counted: ([^(]+)")


def _block_edits_still_hold(project: Project, listed: str) -> bool:
    """Are the named tranches still uncounted? Conservative on anything unreadable.

    A tranche that has vanished entirely leaves the code settled, matching this
    module's stance everywhere else: re-opening every question whose sentence we
    failed to parse would bury the specific reverts worth seeing.
    """
    from tracker.blocks import mw_is_confirmed

    labels = {part.strip().strip("'\"") for part in listed.split(",") if part.strip()}
    if not labels or "none" in labels:
        return True
    by_label = {b.label: b for b in (getattr(project, "blocks", ()) or ())}
    return all(
        block is None or not mw_is_confirmed(block)
        for block in (by_label.get(label) for label in labels)
    )


def _edit_still_holds(project: Project, field: str, expected: str) -> bool:
    """Does the row still carry the value a decision recorded writing?

    Conservative on anything it cannot read: an unparseable note leaves the code
    settled. The failure this exists to catch is a *specific, parseable* revert,
    and re-opening every question whose sentence we failed to parse would bury it.
    """
    if field not in {c.name for c in Project.__table__.columns}:
        return True
    current = getattr(project, field, None)
    if expected.strip().lower() == "empty":
        return current is None
    if current is None:
        return False
    try:
        return abs(float(str(current)) - float(expected.strip().replace(",", ""))) < 0.5
    except (TypeError, ValueError):
        return True


def settled_codes(project: Project) -> set[str]:
    """Finding codes already answered on this row *and still standing*.

    Decisions live in prose in `project.notes`, the one kind of note re-ingesting
    never erases — the same place `logic.record_decision` writes. That is why there
    is no `audit_decision` table: a column would need a migration, would have to be
    kept in step with merges, and would say less than the sentence does.

    **A code is settled only while the edit that settled it survives.** Every action
    in `ACTIONS` writes a project scalar or a block row, and both are caches that
    `upsert.recompute_from_sources` and `upsert.recompute_blocks` re-derive from the
    claim set. So an answered question can come *undone* — and answering by code
    alone then muzzled the detector on exactly the rows where it had most recently
    been right.

    Observed on Hyperion (#10): a model cleared `mw_planned` 13,620 as uncited on
    2026-08-09, `blocks.reconcile` raised it back to 14,462 from the tranche sum,
    and `campus_exceeds_worlds_largest` — which fires on that value — was skipped
    from then on. The row was the worst in the database and the check that would
    have said so had been switched off by its own repair.

    A dismissal is different and stays settled: it records a judgement that the
    figure is right, not an edit that could be reverted.
    """
    settled: set[str] = set()
    for code, what in _DECISION.findall(project.notes or ""):
        edit = _EDIT.match(what.strip())
        if edit and not _edit_still_holds(project, edit.group(1), edit.group(2)):
            continue
        block = _BLOCK_EDIT.match(what.strip())
        if block and not _block_edits_still_hold(project, block.group(1)):
            continue
        settled.add(code)
    return settled


# --- Stage 1: the answers that are arithmetic ----------------------------------


def free_answer(project: Project, finding: UnitFinding) -> tuple[str, str] | None:
    """The key an unattended run may apply without asking anybody, and why.

    Deliberately two cases and no more. Everything else on this list is a judgement
    about which of two sourced figures to believe, and a tool that guesses at that
    is manufacturing facts — which is the failure the whole evidence model exists
    to prevent.
    """
    if finding.code == "h200_disagrees_with_capacity":
        return "r", (
            "h200_equivalent is a pure function of capacity at a fixed kW/H200 ratio, "
            "so re-deriving it is arithmetic and not an opinion"
        )
    if finding.code == "same_figure_two_units":
        # Ruled-out claims are excluded from both sides: a superseded figure is not a
        # rival, and counting its quote would stop this rung ever firing on a row that
        # has already been half-repaired.
        quoted = {
            value
            for value, quote, _, ruled_out in _claims_detail(project, "mw_planned")
            if quote and not ruled_out
        }
        claims = sorted(_claims_for(project, "mw_planned"))
        if claims and quoted == {claims[0]}:
            return "l", (
                f"only the {claims[0]:g} MW claim carries a quote; the "
                f"{claims[-1]:g} figure is cited by nothing"
            )
    if finding.code == "block_out_of_scale":
        blocks = _out_of_scale_blocks(project)
        stated = [_label_states_mw(b) for b in blocks]
        if blocks and all(s is not None for s in stated):
            listed = ", ".join(f"{b.label!r} holds {b.mw:g}" for b in blocks)
            # The label states the true figure a thousandfold below the stored one, so
            # the stored one is not countable. That much is arithmetic. Asserting the
            # label's figure *into* the data is not, and is what this used to do.
            return "u", (
                f"each tranche's own label states its capacity and the stored value is "
                f"exactly a thousand times it ({listed}) — that is the label read as "
                "kilowatts, so the stored figure cannot be counted"
            )
    return None


#: A capacity written into a tranche label: "2.4 MW Lease", "48MW Building 2".
_LABEL_MW = re.compile(r"(\d+(?:\.\d+)?)\s*(mw|megawatt)", re.IGNORECASE)


def _label_states_mw(block: Any) -> float | None:
    """The megawatts a tranche's own label states, when the stored value is that a thousandfold.

    Narrow on purpose. This is the one shape where the correct figure is written
    down beside the wrong one, so no judgement is involved: "2.4 MW Lease" carrying
    2400 is 2.4 MW recorded in kilowatts, and 2.4 is not a guess — the label says
    it. Any other mismatch returns None and the finding goes on down the ladder.
    """
    if block.mw is None:
        return None
    match = _LABEL_MW.search(block.label or "")
    if not match:
        return None
    stated = float(match.group(1))
    if stated <= 0:
        return None
    return stated if abs(block.mw - stated * 1000.0) <= stated else None


def _claims_detail(project: Project, field: str) -> list[tuple[float, str, str, bool]]:
    """(value, quote, url, ruled_out) for every numeric claim made for one field.

    Includes the claims a decision has ruled against, flagged rather than hidden:
    this feeds what a person and a model *read*, and a superseded figure with its
    quote beside it is the record of why the row says what it says.
    """
    from tracker.upsert import _decided_against

    out: list[tuple[float, str, str, bool]] = []
    for source, value in _claim_rows(project, field, live_only=False):
        quote = ""
        try:
            quote = str((json.loads(source.quotes or "{}") or {}).get(field) or "").strip()
        except (TypeError, ValueError):
            quote = ""
        out.append((value, quote, source.url or "", field in _decided_against(source)))
    return out


# --- Stages 3 and 5: asking a model ---------------------------------------------

#: Below this a model's answer is discarded. The same floor `logic` uses for
#: triage, and for the same reason: this edits a row, and the cost of being wrong
#: is not symmetrical with the cost of asking a person.
MIN_CONFIDENCE: Final = 0.6

#: Room for the reasoning models, which think before they answer.
MAX_TOKENS: Final = 8000


@dataclass(frozen=True)
class Verdict:
    """One model answer about one implausible figure."""

    key: str
    confidence: float
    reason: str
    #: "applied" | "declined" | "rejected" | "needs_evidence"
    outcome: str = "applied"
    note: str = ""

    @property
    def acted(self) -> bool:
        return self.outcome == "applied"


def evidence_block(project: Project, finding: UnitFinding) -> str:
    """Every claim behind the figures in question, with its quote and source."""
    lines: list[str] = []
    for name in ("mw_planned", "mw_built", "investment_usd"):
        stored = getattr(project, name, None)
        detail = _claims_detail(project, name)
        if stored is None and not detail:
            continue
        lines.append(f"  {name} = {stored if stored is not None else 'unknown'}")
        for value, quote, url, ruled_out in sorted(detail, key=lambda d: -d[0]):
            tag = " (superseded — ruled against, not in the merge)" if ruled_out else ""
            lines.append(f"      claim {value:g}{tag} — {url[:110] or 'no url'}")
            if quote:
                lines.append(f'        "{quote[:280]}"')
            else:
                lines.append("        (no quote — nothing in the article states this figure)")
    for block in getattr(project, "blocks", ()) or ():
        mw = f"{block.mw:g} MW" if block.mw is not None else "no capacity"
        lines.append(f"  tranche {block.label}: {mw}, {block.status}")
    return "\n".join(lines) or "  (no claims recorded)"


def ask_model(
    project: Project,
    finding: UnitFinding,
    *,
    extractor,
    found_online: str = "",
    prompt_name: str = "audit-resolve-v1",
) -> Verdict:
    """Ask a reasoning model which offered edit applies. One call.

    The model's whole output is one key from a closed set, a confidence and a
    sentence — it cannot type a capacity. `more_evidence` is the fourth answer and
    the reason this is a ladder rather than a single call: "the row does not
    contain the answer" is a *useful* reply, and it is what sends the question to
    the open web instead of forcing a guess out of what is already stored.
    """
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    options = actions_for(finding.code)
    prompt = load_prompt(prompt_name)
    context = {
        "project_id": str(project.id),
        "company": project.company or "unknown",
        "name": project.name or "unknown",
        "location": f"{project.city or project.county or 'unknown'}, {project.state}",
        "phase": project.phase or "unknown",
        "summary": finding.summary,
        "remedy": finding.remedy,
        "evidence": evidence_block(project, finding),
        "found_online": found_online or "  (nothing was searched for yet)",
        "options": "\n".join(f"  {a.key}  {a.label}" for a in options),
    }
    try:
        reply = extractor.complete(
            system=prompt.system, user=prompt.render_user(**context), max_tokens=MAX_TOKENS
        )
    except LLMError as exc:
        log.warning("audit resolve failed for project %s: %s", project.id, exc)
        return Verdict("s", 0.0, "", outcome="rejected", note=f"call failed: {exc}")

    try:
        payload = parse_json_object(reply.text)
    except (LLMJsonError, ValueError):
        return Verdict("s", 0.0, "", outcome="rejected", note="unusable reply")

    key = str(payload.get("key") or "").strip().lower()[:1]
    reason = str(payload.get("reason") or "").strip()
    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    if key == "m":
        # Not a decision and not a failure: the model is saying the row does not
        # hold the answer. Counting this as a decline would hide the one signal
        # that makes searching worth paying for.
        return Verdict("m", confidence, reason, outcome="needs_evidence", note=reason)
    if key == "s" or not key:
        return Verdict(
            "s", confidence, reason, outcome="declined", note=reason or "no option was favoured"
        )
    if key not in {a.key for a in options}:
        return Verdict("s", confidence, reason, outcome="rejected", note=f"{key!r} is not offered")
    if confidence < MIN_CONFIDENCE:
        return Verdict(
            "s",
            confidence,
            reason,
            outcome="declined",
            note=f"confidence {confidence:.2f} is below the {MIN_CONFIDENCE} floor",
        )
    if not reason:
        return Verdict("s", confidence, reason, outcome="rejected", note="no reason given")
    return Verdict(key, confidence, reason)


# --- Stage 4: going and looking -------------------------------------------------

#: Pages fetched per finding. Small on purpose: this runs after two cheaper stages
#: declined, and the question is a single figure — five well-chosen articles either
#: state it or the answer is not on the open web today.
SEARCH_RESULTS: Final = 6
SEARCH_PAGES: Final = 4
#: Characters of each page kept. A whole article would crowd the prompt with
#: paragraphs about other sites.
PAGE_BUDGET: Final = 1400


@dataclass
class Searched:
    """What the web turned up about one figure."""

    queries: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    passages: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def text(self) -> str:
        return "\n\n".join(self.passages)


def search_queries(project: Project, finding: UnitFinding) -> list[str]:
    """What to ask the web about this figure, most specific first."""
    where = project.city or project.county or ""
    base = f"{project.company} {project.name}".strip()
    queries = [f"{base} {where} data center megawatts capacity"]
    if finding.code == "usd_per_mw_out_of_band":
        queries.append(f"{base} {where} data center investment billion")
    if finding.code in {"giant_capacity_unconfirmed", "campus_exceeds_worlds_largest"}:
        queries.append(f'"{project.name}" data center MW {project.state}')
    return [q for q in dict.fromkeys(q.strip() for q in queries) if q]


def find_online(project: Project, finding: UnitFinding, *, settings=None) -> Searched:
    """Search, fetch, and keep the sentences that mention a capacity or a sum.

    Reads pages and stores nothing. That is worth being explicit about: everything
    else in this project that fetches an article writes a `source` row, and this
    deliberately does not — the passages exist to inform one decision, the decision
    is recorded as prose with its reasoning, and a page skimmed for one number is
    not a citation for anything.
    """
    import asyncio

    from tracker.ingest.fetch import fetch_all
    from tracker.ingest.search import SearchError, build_provider, is_useful_host

    got = Searched()
    try:
        provider = build_provider(settings)
    except SearchError as exc:
        got.error = str(exc)
        return got

    hits: list[Any] = []
    for query in search_queries(project, finding):
        got.queries.append(query)
        try:
            hits.extend(provider.search(query, limit=SEARCH_RESULTS))
        except SearchError as exc:
            got.error = str(exc)
            break
        if len(hits) >= SEARCH_RESULTS:
            break

    urls: list[str] = []
    for hit in hits:
        url = getattr(hit, "url", "")
        if url and url not in urls and is_useful_host(url):
            urls.append(url)
    urls = urls[:SEARCH_PAGES]
    if not urls:
        got.error = got.error or "no usable search results"
        return got

    try:
        results = asyncio.run(fetch_all(urls, settings=settings))
    except Exception as exc:
        got.error = f"fetch failed: {exc}"
        return got

    for result in results:
        if not result.ok or not result.markdown:
            continue
        passage = relevant_passage(result.markdown, project)
        if not passage:
            continue
        got.urls.append(result.url)
        got.passages.append(f"FROM {result.url}\n{passage}")
    if not got.passages:
        got.error = got.error or "the pages fetched say nothing about a capacity or a sum"
    return got


#: A sentence worth showing the model: one that carries a capacity, a dollar sum or
#: a chip count. Everything else on a data-center news page is prose about the
#: industry, and it costs tokens to say nothing.
_FIGURE = re.compile(
    r"\b\d[\d,.]*\s*(?:mw|megawatt|gw|gigawatt|kw|kilowatt|billion|million|bn|m\b)",
    re.IGNORECASE,
)


def relevant_passage(article: str, project: Project) -> str:
    """The sentences in one page that carry a figure and name this project."""
    name = (project.name or "").lower()
    company = (project.company or "").lower().split()[0] if project.company else ""
    place = (project.city or project.county or "").lower()
    kept: list[str] = []
    for raw in re.split(r"(?<=[.!?])\s+|\n+", article):
        sentence = " ".join(raw.split())
        if len(sentence) < 40 or not _FIGURE.search(sentence):
            continue
        lowered = sentence.lower()
        if not any(term and term in lowered for term in (name, company, place)):
            continue
        kept.append(sentence)
        if sum(len(s) for s in kept) > PAGE_BUDGET:
            break
    return "\n".join(kept)


# --- The ladder ------------------------------------------------------------------


@dataclass
class Resolution:
    """What happened to one finding, and at which rung."""

    finding: UnitFinding
    #: "arithmetic" | "operator" | "model" | "model-after-search" | "unresolved"
    stage: str = "unresolved"
    key: str = ""
    changed: str = ""
    reason: str = ""
    confidence: float = 0.0
    searched: Searched | None = None
    note: str = ""

    @property
    def acted(self) -> bool:
        return bool(self.changed)

    def as_json(self) -> dict[str, Any]:
        return {
            "project_id": self.finding.project_id,
            "code": self.finding.code,
            "summary": self.finding.summary,
            "stage": self.stage,
            "key": self.key,
            "changed": self.changed,
            "reason": self.reason,
            "confidence": round(self.confidence, 2),
            "searched": bool(self.searched and self.searched.passages),
            "note": self.note,
        }


def apply_action(session: Session, project: Project, finding: UnitFinding, key: str) -> str:
    """Run the chosen edit and return what it changed."""
    action = next(a for a in actions_for(finding.code) if a.key == key)
    return action.apply(session, project, finding)


def record(project: Project, code: str, what: str, *, by: str, detail: str = "") -> None:
    """Write the decision into the row's notes, naming who made it.

    Shares `logic.record_decision` rather than growing a second format, so
    `settled_codes` reads both and a reader meets one sentence shape.
    """
    from tracker.logic import record_decision

    record_decision(project, code, what, by=by, detail=detail)


def resolve_one(
    session: Session,
    project: Project,
    finding: UnitFinding,
    *,
    extractor=None,
    ask=None,
    allow_search: bool = True,
    settings=None,
) -> Resolution:
    """Walk one finding down the ladder until something answers it.

    Args:
        extractor: the reasoning model. None stops the ladder after the operator.
        ask: called with (project, finding, options) and returns a key, "s" to
            skip, or None to hand the question down to the model. The CLI supplies
            the keyboard; a script supplies nothing.
        allow_search: whether a model that says "not in this row" may spend a
            search and four fetches on it.
    """
    got = Resolution(finding=finding)

    # 1. arithmetic
    free = free_answer(project, finding)
    if free is not None:
        key, why = free
        got.stage, got.key, got.reason, got.confidence = "arithmetic", key, why, 1.0
        got.changed = apply_action(session, project, finding, key)
        record(project, finding.code, got.changed, by="rule", detail=why)
        return got

    # 2. the person at the keyboard
    if ask is not None:
        answer = ask(project, finding, actions_for(finding.code))
        if answer == "s":
            got.stage, got.note = "unresolved", "skipped at the keyboard"
            return got
        if answer:
            got.stage, got.key, got.confidence = "operator", answer, 1.0
            got.changed = apply_action(session, project, finding, answer)
            record(project, finding.code, got.changed, by="operator")
            return got

    if extractor is None:
        got.note = "nobody decided, and no model was configured"
        return got

    # 3. the model, on what the row holds
    verdict = ask_model(project, finding, extractor=extractor)
    if verdict.acted:
        got.stage, got.key = "model", verdict.key
        got.reason, got.confidence = verdict.reason, verdict.confidence
        got.changed = apply_action(session, project, finding, verdict.key)
        record(
            project,
            finding.code,
            got.changed,
            by=f"model ({verdict.confidence:.2f})",
            detail=verdict.reason,
        )
        return got

    # 4 and 5. the open web, then the model again
    if verdict.outcome == "needs_evidence" and allow_search:
        got.searched = find_online(project, finding, settings=settings)
        if got.searched.passages:
            second = ask_model(
                project, finding, extractor=extractor, found_online=got.searched.text
            )
            if second.acted:
                got.stage, got.key = "model-after-search", second.key
                got.reason, got.confidence = second.reason, second.confidence
                got.changed = apply_action(session, project, finding, second.key)
                record(
                    project,
                    finding.code,
                    got.changed,
                    by=f"model after search ({second.confidence:.2f})",
                    detail=f"{second.reason} [read {', '.join(got.searched.urls[:2])}]",
                )
                return got
            got.note = second.note or "the model declined again after reading"
            return got
        got.note = got.searched.error or "the search found nothing about this figure"
        return got

    got.note = verdict.note or "the model declined"
    return got


__all__ = [
    "ACTIONS",
    "DISMISS",
    "GIANT_UNCONFIRMED_MW",
    "MAX_TOKENS",
    "MIN_CONFIDENCE",
    "SINGLE_CAMPUS_CEILING_MW",
    "USD_PER_MW_CEILING",
    "USD_PER_MW_FLOOR",
    "Action",
    "Resolution",
    "Searched",
    "UnitFinding",
    "Verdict",
    "actions_for",
    "apply_action",
    "ask_model",
    "check_project",
    "evidence_block",
    "find_online",
    "free_answer",
    "record",
    "relevant_passage",
    "resolve_one",
    "run",
    "search_queries",
    "settled_codes",
]
