"""The figures beside the console's map: totals, stages and states, counted like
the Capex page.

The dots themselves are the console's own dataset (`/api/dataset`'s `projects`);
what the browser cannot work out alone is which rows the Capex page counts, so that
is decided here, and the rows not to draw are sent as ids.

**The Capex page's campuses, each counted once.** The totals and each state's
figures count the campuses `capex.rollup` counts — paused and cancelled ones left
out, one row per suspected duplicate — and each at its own planned capacity. The
Capex page's buyer rows can add up to more: a campus whose buildings are listed by
buyer credits each buyer with its buildings, and on a few campuses those buildings
sum past the campus's own figure (Stargate Abilene: 3,672 MW of buildings, 1,200 MW
for the campus). The map takes the campus's word, the lower of the two.

A suspected duplicate's second row is not drawn either (`skipped`): two dots for one
campus would be the double count again, in a picture. Every capacity is a floor: a
campus whose size nobody published counts as zero, and the page says so.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.models import Project

#: The stages, in the order the page lists them.
STAGES: Final = ("announced", "permitting", "construction", "operational", "paused", "cancelled")


def counted_ids(session: Session) -> tuple[set[int], set[int]]:
    """(the ids the Capex page counts, the ids it sets aside as duplicates)."""
    from tracker import capex as capex_mod

    with capex_mod.working_set(session):
        pairs = capex_mod.suspected_duplicates(session)
        positions = capex_mod.rollup(session, pairs=pairs)
    counted = {pid for position in positions for pid in position.project_ids}
    skipped = {pid for position in positions for pid in position.duplicate_skipped_ids}
    return counted, skipped


def build(session: Session) -> dict[str, Any]:
    """Totals, stage counts and per-state figures, and the rows not to draw."""
    counted, skipped = counted_ids(session)
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
