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
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import Project

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

#: `mw_planned` above this on a row whose figure is 待确认 (no quote anywhere
#: names it) is flagged. A gigawatt claim resting on nothing quotable deserves a
#: human eye before it deserves a place in a total.
GIANT_UNCONFIRMED_MW = 1_000


@dataclass(frozen=True)
class UnitFinding:
    project_id: int
    name: str
    code: str
    summary: str
    #: What to do about it — every finding must say, or it is just an alarm.
    remedy: str


def _claims_for(project: Project, field: str) -> list[float]:
    """Every numeric value any source ever claimed for one field."""
    out: list[float] = []
    for source in project.sources:
        if not source.claims:
            continue
        try:
            value = json.loads(source.claims).get(field)
        except (TypeError, ValueError):
            continue
        if isinstance(value, (int, float)) and value > 0:
            out.append(float(value))
    return out


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
        "h200_disagrees_with_capacity": 5,
    }
    findings.sort(key=lambda f: (order.get(f.code, 9), f.project_id))
    return findings


__all__ = [
    "GIANT_UNCONFIRMED_MW",
    "SINGLE_CAMPUS_CEILING_MW",
    "USD_PER_MW_CEILING",
    "USD_PER_MW_FLOOR",
    "UnitFinding",
    "check_project",
    "run",
]
