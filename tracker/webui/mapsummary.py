"""The figures beside the console's map: totals, stages and states, counted like
the Capex page.

The dots themselves are the console's own dataset (`/api/dataset`'s `projects`);
what the browser cannot work out alone is which rows the Capex page counts, so that
is decided here, and the rows not to draw are sent as ids.

**The Capex page's campuses, each counted once.** The totals and each state's
figures count the campuses `capex.rollup` counts — paused and cancelled ones left
out, one row per suspected duplicate — read off the same rollup (`capex.tally`), so
the two pages cannot disagree about which rows exist.

**Each at its own planned capacity, and that is the one difference.** The Capex
page credits a buyer with the buildings listed for it, and the company pages
divide the Capex page's figures among companies, so those two add up to the same
total. On a few campuses the buildings listed by buyer sum past the campus's own
figure (Stargate Abilene: 3,672 MW of buildings, 1,200 MW for the campus), and
there the Capex and company totals are higher than the map's by exactly that
excess. The map takes the campus's word, the lower of the two: a state's shading
is a statement about the campus, not about who has leased its halls.

A suspected duplicate's second row is not drawn either (`skipped`): two dots for one
campus would be the double count again, in a picture. Every capacity is a floor: a
campus whose size nobody published counts as zero, and the page says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.models import Project

if TYPE_CHECKING:
    from tracker.capex import Tally

#: The stages, in the order the page lists them.
STAGES: Final = ("announced", "permitting", "construction", "operational", "paused", "cancelled")


def counted_ids(session: Session) -> tuple[set[int], set[int]]:
    """(the ids the Capex page counts, the ids it sets aside as duplicates)."""
    from tracker import capex as capex_mod

    tally = capex_mod.counting(session)
    return set(tally.counted), set(tally.set_aside)


def build(session: Session, *, tally: Tally | None = None) -> dict[str, Any]:
    """Totals, stage counts and per-state figures, and the rows not to draw.

    `tally` is the Capex page's rollup read back as rows (`capex.tally`), which the
    console holds cached; without one the rollup is run here.
    """
    if tally is None:
        counted, skipped = counted_ids(session)
    else:
        counted, skipped = set(tally.counted), set(tally.set_aside)
    projects = session.scalars(select(Project).order_by(Project.id)).all()

    states: dict[str, dict[str, Any]] = {}
    totals = {"campuses": 0, "planned_mw": 0.0, "operating_mw": 0.0, "states": 0}
    stages = dict.fromkeys(STAGES, 0)
    for project in projects:
        if project.id in skipped:
            continue
        if project.phase in stages:
            stages[project.phase] += 1
        if project.id not in counted:
            continue
        totals["campuses"] += 1
        totals["planned_mw"] += float(project.mw_planned or 0.0)
        totals["operating_mw"] += float(project.mw_built or 0.0)
        row = states.setdefault(project.state, {"campuses": 0, "planned_mw": 0.0})
        row["campuses"] += 1
        row["planned_mw"] += float(project.mw_planned or 0.0)
    totals["states"] = len(states)
    totals["planned_mw"] = round(totals["planned_mw"], 1)
    totals["operating_mw"] = round(totals["operating_mw"], 1)
    for row in states.values():
        row["planned_mw"] = round(row["planned_mw"], 1)
    updated = session.scalar(select(func.max(Project.updated_at)))
    return {
        "updated_at": updated.date().isoformat() if updated else None,
        "totals": totals,
        "stages": stages,
        "states": dict(sorted(states.items())),
        "skipped": sorted(skipped),
    }


__all__ = ["STAGES", "build", "counted_ids"]
