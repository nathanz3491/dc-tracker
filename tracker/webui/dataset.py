"""The payload the console renders, assembled from the modules that own each part.

Nothing here restates a rule that lives elsewhere. Tracks come from `tracker.tracks`,
vocabularies from `tracker.vocab`, source weights from `tracker.confidence`, field
coverage from `tracker.gaps`, the queue from `tracker.ingest.discover`. If a rule
were copied into this file it would be a second definition free to drift from the
one the CLI enforces, and the whole value of the console is that it shows the same
judgements the commands make.

The shape is the mockup's ``window.DCTRACKER``, so the ported view code needs no
translation layer.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker import __version__
from tracker import required as required_list
from tracker.confidence import SOURCE_WEIGHTS
from tracker.export import fetch_projects, to_json_object
from tracker.gaps import measure as measure_gaps
from tracker.gaps import worst as worst_gaps
from tracker.models import IngestUrl
from tracker.tracks import RISK_TRACK, TRACK_LABELS, TRACK_MILESTONES, TRACKS
from tracker.vocab import (
    EVENT_TYPES,
    PHASES,
    RISK_CATEGORIES,
    RISK_SEVERITIES,
    SOURCE_TYPES,
    TRACKED_FIELDS,
)

log = logging.getLogger(__name__)

#: How many queued candidates and failed URLs to ship. The queue is a triage
#: surface, not an archive; an operator who wants all of it has `tracker queue`.
QUEUE_LIMIT = 200


def _iso_of(project) -> str | None:
    """Which interconnection queue this project came out of, if any.

    Read off the ISO ingest's extractor stamp (`pjm:v3:sha256=…`) rather than
    guessed from the state. Several states are split between ISOs and one is
    served by none, so a state->ISO table would be an invention presented as a
    fact — the same objection `ingest geo` raises against guessing a county for a
    city that spans four.
    """
    for source in project.sources:
        if source.source_type != "iso_queue":
            continue
        stamp = (source.extractor or "").split(":", 1)[0].strip().lower()
        if stamp:
            return stamp.upper()
    return None


def _nulls(project) -> dict[str, dict[str, Any]]:
    """Why each empty tracked field is empty.

    A NULL is not always a gap, and the table has been unable to say so. Most of
    the dashes on screen are correct answers — `mw_built` on a project that has
    not broken ground, `customer` on a self-built campus — and rendering them
    identically to a genuinely unknown value makes the dataset look thin when it
    is merely honest.

    `gaps.for_project` already draws that line and nothing was reading it.
    """
    from tracker.gaps import FILLED, for_project
    from tracker.vocab import TRACKED_FIELDS

    out: dict[str, dict[str, Any]] = {}
    for state in for_project(project, TRACKED_FIELDS):
        if state.status == FILLED:
            continue
        out[state.field] = {"status": state.status, "reason": state.reason}
    return out


def _unconfirmed_because(project) -> dict[str, dict[str, str]]:
    """Why a 待确认 value is 待确认, where the ingest path recorded a reason.

    One tier, two causes, and they call for opposite work. The usual one is that
    nothing quotable backs the value, and the answer is another source. The other
    is that the quote is real and the figure is not this site's — a programme
    total lifted from an article about one campus — and the answer is to correct
    it. Showing both as the same amber chip tells a reader to go looking for a
    citation that already exists.

    The reason is not a column; it is the disclosure `crawl.py` writes into the
    project's notes when the ratio check fires. Read back rather than recomputed,
    so what the console shows is what the ingest actually decided — recomputing
    the ratio from the merged values would sometimes accuse a figure no gate ever
    demoted.
    """
    from tracker.ingest.crawl import SCALE_NOTE_FIELD, SCALE_NOTE_MARKER
    from tracker.upsert import SOURCE_NOTE_PREFIX

    out: dict[str, dict[str, str]] = {}
    for raw in (project.notes or "").splitlines():
        line = raw.strip()
        if SCALE_NOTE_MARKER not in line:
            continue
        # Strip the `[source][tag] ` bookkeeping; the sentence is the useful part.
        if line.startswith(SOURCE_NOTE_PREFIX):
            line = line.split("] ", 1)[-1].strip()
        out[SCALE_NOTE_FIELD] = {"code": "scale", "note": line}
    return out


def _queue(session: Session) -> list[dict[str, Any]]:
    from tracker.ingest import discover

    identities = discover.project_identities(session)
    implied = discover.newsroom_companies()
    rows = discover.pending(session, limit=QUEUE_LIMIT)
    return [
        {
            "url": row.url,
            "title": row.title,
            "feed": row.feed,
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "status": row.status,
            # `depth` is what the crawl path's depth-first ordering keys on: this
            # candidate is about a project already tracked, so reading it deepens a
            # row rather than adding another single-source one.
            "depth": discover.matches_known_project(
                row.url, row.title, identities, implied_companies=implied
            ),
        }
        for row in rows
    ]


def _failed(session: Session) -> list[dict[str, Any]]:
    """Unreadable URLs grouped by host, because the cause is almost always the host.

    34 separate 403s from one Cloudflare-fronted domain is one fact, not 34. Listing
    them individually buried the two hosts that were merely rate-limited and could
    be retried.
    """
    from urllib.parse import urlsplit

    from tracker.ingest import discover

    grouped: dict[str, dict[str, Any]] = {}
    for row in discover.failed(session, limit=QUEUE_LIMIT):
        host = urlsplit(row.url).netloc or "?"
        entry = grouped.setdefault(
            host,
            {"host": host, "count": 0, "http_status": row.http_status, "statuses": {}, "urls": []},
        )
        entry["count"] += 1
        if row.http_status:
            entry["statuses"][str(row.http_status)] = (
                entry["statuses"].get(str(row.http_status), 0) + 1
            )
        if len(entry["urls"]) < 5:
            entry["urls"].append({"url": row.url, "status": row.status, "error": row.error})
    for entry in grouped.values():
        if entry["statuses"]:
            entry["http_status"] = int(max(entry["statuses"], key=lambda k: entry["statuses"][k]))
    return sorted(grouped.values(), key=lambda e: (-e["count"], e["host"]))


def _gaps(session: Session) -> dict[str, Any]:
    gaps = measure_gaps(session)
    return {
        "fields": [
            {
                "field": g.field,
                "filled": g.filled,
                "applicable": g.applicable,
                "missing": g.missing,
                "pct": g.pct,
                "measurable": g.measurable,
                "note": g.note,
            }
            for g in gaps
        ],
        "worst": [g.field for g in worst_gaps(gaps)],
    }


def _capex(session: Session) -> dict[str, Any]:
    """Capacity by the company buying it, plus what would make it wrong.

    The duplicate warning ships with this and not with `gaps`, for the reason
    `capex.suspected_duplicates` gives: a row stored twice is a nuisance in a
    site listing and a wrong number the moment anything groups by end customer.
    Abilene is in the database four times, so 1.2 GW is counted four times
    against OpenAI. The place to offer the repair is next to the figure it
    corrupts.

    Groups carry ids only. Every project is already in the payload, so the page
    looks the rows up rather than being sent a second, driftable copy of them.
    """
    from tracker import capex as capex_mod

    positions = capex_mod.rollup(session)
    pairs = capex_mod.suspected_duplicates(session)
    return {
        "coverage": capex_mod.coverage(session),
        "years": capex_mod.horizon(positions),
        "quarters": capex_mod.quarters(positions),
        "date_precision": capex_mod.date_precision(session),
        "as_of_year": capex_mod.as_of().year,
        "as_of_quarter": f"{capex_mod.as_of().year}Q{(capex_mod.as_of().month - 1) // 3 + 1}",
        "unattributed": capex_mod.UNATTRIBUTED,
        "positions": [
            {
                "customer": p.name,
                "key": p.key,
                "projects": p.projects,
                "self_built": p.self_built,
                "undisclosed": p.undisclosed,
                "mw_planned": p.mw_planned,
                "mw_built": p.mw_built,
                "mw_unbuilt": p.mw_unbuilt,
                "investment_usd": p.investment_usd,
                "mw_by_year": {str(y): mw for y, mw in sorted(p.mw_by_year.items())},
                "mw_by_quarter": dict(sorted(p.mw_by_quarter.items())),
                "projects_at_risk": p.at_risk_projects,
                "mw_at_risk": p.mw_at_risk,
                "slipped": p.slipped,
                "worst_open_risk": capex_mod.blocking_risk(session, p.key) if p.key else None,
                "phases": p.phases,
            }
            for p in positions
        ],
        "suspect": [
            {"id": pid, "operator": operator, "customer": customer}
            for pid, operator, customer in capex_mod.suspect_attributions(session)
        ],
        "duplicates": {
            "groups": capex_mod.duplicate_groups(pairs),
            "double_counted_mw": capex_mod.double_counted_mw(pairs),
        },
    }


def _required(session: Session, projects) -> dict[str, Any]:
    wanted = required_list.load()
    matches = required_list.match(projects, wanted)
    return {
        "path": str(required_list.default_path()),
        "target": 30,
        "entries": [{"entry": m.entry, "id": m.project_id, "met": m.met} for m in matches],
    }


def _feeds() -> list[dict[str, Any]]:
    from tracker.ingest import discover

    try:
        feeds, _filter = discover.load_config()
    except Exception:
        # A broken or missing feeds.toml is a discovery problem, not a reason the
        # projects table should fail to render.
        log.warning("could not read the feed configuration; the feeds panel will be empty")
        return []
    return [
        {
            "name": f.name,
            "url": f.url,
            "source_type": f.source_type,
            "topic_implied": f.topic_implied,
        }
        for f in feeds
    ]


def _risk_exposure(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cited capacity behind each open obstacle category.

    A project appears under every category obstructing it, so these do not sum to a
    fleet total — the same caveat `tracker exposure` prints, carried here because a
    bar chart invites exactly that misreading. `no_mw` counts the projects with an
    open risk and no cited capacity: excluded from the total rather than treated as
    zero, which would understate the exposure while looking precise.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for project in projects:
        open_risks = [r for r in project["risks"] if r["status"] == "open"]
        for risk in open_risks:
            entry = buckets.setdefault(
                risk["category"],
                {
                    "category": risk["category"],
                    "track": RISK_TRACK.get(risk["category"]),
                    "projects": 0,
                    "mw": 0.0,
                    "no_mw": 0,
                    "by_severity": dict.fromkeys(RISK_SEVERITIES, 0.0),
                },
            )
            entry["projects"] += 1
            if project["mw_planned"] is None:
                entry["no_mw"] += 1
            else:
                entry["mw"] += project["mw_planned"]
                entry["by_severity"][risk["severity"]] += project["mw_planned"]
    return sorted(buckets.values(), key=lambda e: (-e["mw"], -e["projects"], e["category"]))


def build(session: Session, *, db_path: str, schema_version: int) -> dict[str, Any]:
    """The whole console payload for one request."""
    rows = fetch_projects(session)
    projects = []
    for project in rows:
        payload = to_json_object(project)
        payload["iso"] = _iso_of(project)
        payload["nulls"] = _nulls(project)
        payload["unconfirmed_because"] = _unconfirmed_because(project)
        payload["filled"] = sum(1 for f in TRACKED_FIELDS if getattr(project, f, None) is not None)
        projects.append(payload)

    citations = sum(len(p["sources"]) for p in projects)
    queued = session.scalar(select(IngestUrl.id).where(IngestUrl.status == "discovered").limit(1))

    return {
        "schema": "tracker/webui-1",
        "db": db_path,
        "schema_version": schema_version,
        "version": __version__,
        "projects": projects,
        "totals": {
            "projects": len(projects),
            "citations": citations,
            "states": len({p["state"] for p in projects}),
            "mw_planned": sum(p["mw_planned"] or 0 for p in projects),
            "mw_cited_projects": sum(1 for p in projects if p["mw_planned"] is not None),
            "investment_usd": sum(p["investment_usd"] or 0 for p in projects),
            "queue_has_work": queued is not None,
        },
        "exposure": _risk_exposure(projects),
        "capex": _capex(session),
        "queue": _queue(session),
        "failed": _failed(session),
        "gaps": _gaps(session),
        "required": _required(session, rows),
        "feeds": _feeds(),
        # --- reference data, every entry owned by another module -------------
        "tracks": [
            {"key": t, "label": TRACK_LABELS[t], "milestones": list(TRACK_MILESTONES[t])}
            for t in TRACKS
        ],
        "riskTrack": dict(RISK_TRACK),
        "riskCategories": list(RISK_CATEGORIES),
        "riskSeverities": list(RISK_SEVERITIES),
        "phases": list(PHASES),
        "eventTypes": list(EVENT_TYPES),
        "sourceTypes": list(SOURCE_TYPES),
        "sourceWeight": dict(SOURCE_WEIGHTS),
        "trackedFields": list(TRACKED_FIELDS),
    }


__all__ = ["build"]
