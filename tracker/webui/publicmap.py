"""The public map: where the campuses are, how big, at what stage — for anybody.

**An allowlist, built here, not a filter in the browser.** The page is public and
indexed, so whatever this module returns can be scraped; the console's `/api/*`
payloads are never reused, because the browser would receive every field of every
row and merely not draw most of them. `FIELDS` is the whole of what each detail
level may carry, and `point` builds each dot from exactly those keys. There is no
level at which a stranger gets investment, a tenant, a date, an obstacle, a quote or
a source: those are what the console is for.

=========  =================================================================
`states`   totals and the shaded states; no dots
`dots`     also a dot per campus: operator, state, stage, a capacity band,
           a position rounded to 0.1° (about 11 km)
`campuses` also the campus's name, its town and its exact planned capacity,
           at 0.01°
=========  =================================================================

The level is `Settings.public_map_detail`, so changing what strangers see is a line
in `.env`, not a deploy.

**The Capex page's campuses, each counted once.** The totals and each state's
figures count the campuses `capex.rollup` counts — paused and cancelled ones left
out, one row per suspected duplicate — and each at its own planned capacity. The
Capex page's buyer rows can add up to more: a campus whose buildings are listed by
buyer credits each buyer with its buildings, and on 11 campuses those buildings sum
past the campus's own figure (Stargate Abilene: 3,672 MW of buildings, 1,200 MW
for the campus). A stranger's total takes the campus's word, the lower of the two.
A suspected duplicate's second row is not drawn either: two dots for one campus
would be the double count again, in a picture. Every capacity is a floor: a campus
whose size nobody published counts as zero, and the page says so.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.models import Project

#: What each detail level may say about one campus, and nothing else.
FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "states": (),
    "dots": ("lat", "lon", "stage", "band", "operator", "state"),
    "campuses": ("lat", "lon", "stage", "band", "operator", "state", "name", "place", "mw"),
}

LEVELS: Final = tuple(FIELDS)

#: The stages, in the order the legend reads.
STAGES: Final = ("announced", "permitting", "construction", "operational", "paused", "cancelled")

#: Stages left out of the totals, as on the Capex page (`capex.PHASE_TERMINAL`).
TERMINAL: Final = frozenset({"paused", "cancelled"})

#: Capacity bands, as (key, lower bound in MW). A dot is sized by its band, so the
#: `dots` level never sends a figure more exact than the band it falls in.
BANDS: Final[tuple[tuple[str, float], ...]] = (
    ("under_100", 0.0),
    ("100_299", 100.0),
    ("300_999", 300.0),
    ("1000_2999", 1000.0),
    ("3000_plus", 3000.0),
)

#: Decimal places a position keeps at each level.
PRECISION: Final = {"dots": 1, "campuses": 2}


def band(mw: float | None) -> str | None:
    """The band a planned capacity falls in, or None when nobody published one."""
    if mw is None or mw <= 0:
        return None
    key = BANDS[0][0]
    for name, floor in BANDS:
        if mw >= floor:
            key = name
    return key


def _place(project: Project) -> str | None:
    if project.city:
        return project.city
    if project.county:
        return project.county
    return None


def point(project: Project, level: str) -> dict[str, Any]:
    """One dot, carrying exactly `FIELDS[level]`."""
    places = PRECISION[level]
    full = {
        "lat": round(float(project.lat), places),
        "lon": round(float(project.lon), places),
        "stage": project.phase,
        "band": band(project.mw_planned),
        "operator": project.company,
        "state": project.state,
        "name": project.name,
        "place": _place(project),
        "mw": round(float(project.mw_planned), 1) if project.mw_planned else None,
    }
    return {key: full[key] for key in FIELDS[level]}


def counted_ids(session: Session) -> tuple[set[int], set[int]]:
    """(the ids the Capex page counts, the ids it sets aside as duplicates)."""
    from tracker import capex as capex_mod

    with capex_mod.working_set(session):
        pairs = capex_mod.suspected_duplicates(session)
        positions = capex_mod.rollup(session, pairs=pairs)
    counted = {pid for position in positions for pid in position.project_ids}
    skipped = {pid for position in positions for pid in position.duplicate_skipped_ids}
    return counted, skipped


def build(session: Session, *, level: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """The map's whole payload at `level`."""
    if level not in FIELDS:
        raise ValueError(f"no public map level {level!r}; one of {', '.join(LEVELS)}")
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

    points: list[dict[str, Any]] = []
    if FIELDS[level]:
        points = [
            point(p, level)
            for p in projects
            if p.id not in skipped and p.lat is not None and p.lon is not None
        ]
    updated = session.scalar(select(func.max(Project.updated_at)))
    return {
        "level": level,
        "generated_at": (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds"),
        "updated_at": updated.date().isoformat() if updated else None,
        "totals": totals,
        "stages": stages,
        "states": dict(sorted(states.items())),
        "bands": [name for name, _floor in BANDS],
        "points": points,
    }


def _gw(mw: float) -> str:
    if mw <= 0:
        return "not published"
    return f"{mw / 1000:,.1f} GW" if mw >= 1000 else f"{mw:,.0f} MW"


def summary_html(data: dict[str, Any]) -> str:
    """The headline figures and the state table, as markup for the page shell.

    Written on the server, in English, so the page says something to a reader
    without JavaScript and to a search engine; the script replaces it with the same
    figures in the reader's language. Every value is escaped, though none comes from
    a request.
    """
    from html import escape

    totals = data["totals"]
    states = sorted(data["states"].items(), key=lambda kv: (-kv[1]["planned_mw"], kv[0]))
    rows = "".join(
        f'<tr><th scope="row">{escape(code)}</th><td>{row["campuses"]:,}</td>'
        f"<td>{escape(_gw(row['planned_mw']))}</td></tr>"
        for code, row in states
    )
    return (
        '<dl class="figures">'
        f"<div><dt>Campuses tracked</dt><dd>{totals['campuses']:,}</dd></div>"
        f"<div><dt>Capacity planned</dt><dd>{escape(_gw(totals['planned_mw']))}</dd></div>"
        f"<div><dt>Capacity operating</dt><dd>{escape(_gw(totals['operating_mw']))}</dd></div>"
        f"<div><dt>States</dt><dd>{totals['states']:,}</dd></div>"
        "</dl>"
        '<table class="state-table"><caption>By state, planned capacity first</caption>'
        '<thead><tr><th scope="col">State</th><th scope="col">Campuses</th>'
        '<th scope="col">Planned</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


__all__ = [
    "BANDS",
    "FIELDS",
    "LEVELS",
    "STAGES",
    "band",
    "build",
    "counted_ids",
    "point",
    "summary_html",
]
