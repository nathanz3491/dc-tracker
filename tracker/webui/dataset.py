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

import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker import __version__
from tracker import required as required_list
from tracker.confidence import SOURCE_WEIGHTS
from tracker.export import fetch_projects, iso, to_json_object
from tracker.gaps import measure as measure_gaps
from tracker.gaps import worst as worst_gaps
from tracker.models import IngestUrl
from tracker.tracks import RISK_TRACK, TRACK_LABELS, TRACK_MILESTONES, TRACKS
from tracker.upsert import blocker_rationale
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


#: What each refusal means to somebody looking at the chip, and what it asks of
#: them. The distinction is the point: two of these send you to find a source,
#: and two send you to correct something you already have.
_REASON_NOTES: dict[str, str] = {
    "no_quote": "the source asserted this and quoted nothing for it",
    "quote_unverified": "the quote offered for this is not in the article",
    "quote_off_target": "the article's sentence for this does not state this value",
    "out_of_scale": "quoted, but implausible for a site this size — usually a "
    "programme-wide total quoted in an article about one campus",
}


def _unconfirmed_because(project) -> dict[str, dict[str, str]]:
    """Why a 待确认 value is 待确认, as the ingest gate recorded it.

    One tier, several causes, and they call for opposite work. The usual one is
    that nothing quotable backs the value, and the answer is another source. The
    other is that the quote is real and the figure is not this site's — a
    programme total lifted from an article about one campus — and the answer is
    to correct it. Showing both as the same amber chip tells a reader to go
    looking for a citation that already exists.

    Read from `source.unconfirmed_reasons` (migration 0013), never recomputed:
    recomputing the ratio from the merged values would sometimes accuse a figure
    no gate ever demoted. This used to reconstruct the one reason it could by
    string-matching a marker in the project's notes, which could only ever see
    the scale demotion; the column is that seam made explicit.

    A source older than 0013 has no reason recorded, and gets no chip rather than
    a guessed one.
    """
    out: dict[str, dict[str, str]] = {}
    for source in project.sources:
        if not source.unconfirmed_reasons:
            continue
        try:
            reasons = json.loads(source.unconfirmed_reasons)
        except (TypeError, ValueError):
            continue
        if not isinstance(reasons, dict):
            continue
        for field_name, code in reasons.items():
            note = _REASON_NOTES.get(code)
            # An unrecognised code is a newer writer than this reader. Say
            # nothing rather than invent a gloss for it.
            if note and field_name not in out:
                out[field_name] = {"code": code, "note": note}
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
    Abilene was in the database four times, and 1.2 GW was counted four times
    against OpenAI until the rollup learned to skip the extra rows. The place to
    offer the real repair — the merge — is still next to the figure it corrupts.

    Groups carry ids only. Every project is already in the payload, so the page
    looks the rows up rather than being sent a second, driftable copy of them.

    `year_columns` / `quarter_columns` are computed here rather than in the
    browser: which years the grid shows — including an empty 2029 between a
    dated 2028 and 2030 — is a judgement, and the browser never re-implements a
    judgement (docs/architecture.md).
    """
    from tracker import capex as capex_mod

    positions = capex_mod.rollup(session)
    pairs = capex_mod.suspected_duplicates(session)
    as_of = capex_mod.as_of()
    as_of_quarter = f"{as_of.year}Q{(as_of.month - 1) // 3 + 1}"
    return {
        "coverage": capex_mod.coverage(session),
        "years": capex_mod.horizon(positions),
        "quarters": capex_mod.quarters(positions),
        "year_columns": capex_mod.year_columns(positions, start=as_of.year),
        "quarter_columns": capex_mod.quarter_columns(positions, start=as_of_quarter),
        "date_precision": capex_mod.date_precision(session),
        # How many stored capacities say which KIND of megawatt they are. The
        # `unrecorded` bucket is the one to render: those figures are not
        # comparable with the ones that do say, and the share is the measurement
        # the axis was added to make. Counted here rather than in the page for
        # the reason this module opens with — two definitions of one number are
        # free to disagree.
        "basis_census": capex_mod.basis_census(session),
        "basis_unrecorded_key": capex_mod.BASIS_UNRECORDED,
        # One dollar figure standing as the cost of several of an operator's
        # campuses at once, which a site cost cannot be. Counted in the sums
        # above and flagged, on the same terms as a suspected duplicate: the
        # repair is a person correcting the figure or superseding the claim.
        "programme_figures": [
            {
                "operator": f.operator,
                "investment_usd": f.investment_usd,
                "sites": f.sites,
                "project_ids": list(f.project_ids),
            }
            for f in capex_mod.programme_figures(session)
        ],
        "as_of_year": as_of.year,
        "as_of_quarter": as_of_quarter,
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
                "investment_excluded_usd": p.investment_excluded_usd,
                "investment_unquoted_usd": p.investment_unquoted_usd,
                # Money the article says is a programme's, a region's or the
                # operator's whole estate rather than this site's. Quoted
                # correctly and excluded from the sum, which is a different
                # statement from `investment_excluded_usd` above: that one is the
                # $/MW ceiling refusing an implausible figure, this one is the
                # article saying in words what the figure is a figure of.
                "investment_out_of_scope_usd": p.investment_out_of_scope_usd,
                "duplicate_rows_skipped": p.duplicate_rows_skipped,
                "mw_duplicate_skipped": p.mw_duplicate_skipped,
                "investment_duplicate_skipped_usd": p.investment_duplicate_skipped_usd,
                # Ids only, like the duplicate groups: the page looks the rows up
                # in the projects payload rather than being sent a second copy.
                "project_ids": p.project_ids,
                "duplicate_skipped_ids": p.duplicate_skipped_ids,
                "mw_by_year": {str(y): mw for y, mw in sorted(p.mw_by_year.items())},
                "mw_by_quarter": dict(sorted(p.mw_by_quarter.items())),
                "projects_at_risk": p.at_risk_projects,
                "mw_at_risk": p.mw_at_risk,
                "projects_at_risk_unconfirmed": p.at_risk_unconfirmed,
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
            # The tranches two rows hold in common, keyed by the pair. A derived
            # `block_key` on both rows is far harder evidence than a name
            # resemblance, and an operator deciding whether to merge should see the
            # strongest argument rather than infer it.
            "shared_blocks": {
                f"{p.a_id}-{p.b_id}": list(p.shared_blocks) for p in pairs if p.shared_blocks
            },
            # What raised each pair, and what each group is best described by, both
            # named by the backend. There are five evidence classes and they are not
            # equal — one carries an unattended merge and another is a word — so the
            # page has to be able to say which without deriving it, per the rule in
            # `docs/architecture.md`: a judgement is computed once and drawn twice.
            # `capex.strongest_evidence` is the same function the CLI's report calls.
            "evidence": {
                f"{p.a_id}-{p.b_id}": {"kinds": list(p.kinds), "why": p.why} for p in pairs
            },
            "group_evidence": [
                {
                    "ids": ids,
                    "kind": capex_mod.strongest_evidence(pairs, ids),
                    "label": capex_mod.EVIDENCE_LABELS.get(
                        capex_mod.strongest_evidence(pairs, ids), "same locality"
                    ),
                }
                for ids in capex_mod.duplicate_groups(pairs)
            ],
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


def _split_serving(payload: dict[str, Any]) -> None:
    """Move the utility's plant out of the campus list, into its own.

    Hyperion (#10) records 5,962 MW of Entergy gas units, a solar farm and a
    nuclear uprate as tranches of the campus — including a reactor 250 miles away.
    Every *sum* has excluded them since `blocks.is_generation` was written, so the
    figures are right; the tranche list is where they are still filed wrongly, and
    a reader looking at eighteen rows has no way to tell six of them are a
    different quantity.

    They are moved rather than dropped. Entergy building 2,262 MW of gas *for this
    campus* is one of the most important facts about it — it belongs under power,
    not under capacity, and deleting it would lose the fact to fix the filing.

    Split with the predicate the sums use, on each row's own label. Never a second
    copy of that word list: a display that disagreed with the arithmetic about what
    counts as generation would put a plant in one place and its megawatts in
    another — which is what the console does today, adding Entergy's running gas
    units into the campus's "delivering" figure while the reconciliation three rows
    below correctly excludes them.

    Both lists are split, not just one. `blocks` is what the tab counts and what it
    sums into the bar; `sections` is what it draws as rows. Splitting either alone
    leaves the two disagreeing.

    Only one of the two halves is kept. The generation rows pulled out of `blocks`
    used to ship as `serving_blocks` beside the ones pulled out of `sections`, and
    nothing ever read them — the plant list on the page is drawn from `serving`.
    Two copies of one fact, one of them dead weight in every payload.
    """
    from tracker import blocks as blocks_mod

    def generation(row: dict[str, Any]) -> bool:
        return blocks_mod.is_generation(row.get("label"), row.get("parent"))

    for key, moved_to in (("blocks", None), ("sections", "serving")):
        rows = payload.get(key) or []
        payload[key] = [r for r in rows if not generation(r)]
        if moved_to is not None:
            payload[moved_to] = [r for r in rows if generation(r)]


def _kw_per_h200() -> float:
    from tracker.compute import kw_per_h200

    return kw_per_h200()


def project_payload(project: Any, *, claims: bool = False) -> dict[str, Any]:
    """One project as the console reads it: the export shape, plus what the page adds.

    **Shared by the list and the single-project route on purpose.** `/api/dataset`
    sends every project for the table and `/api/project` sends one for its own
    page, and both have to agree about what a project *is* — two copies of this
    decoration would drift, and the page would then show a different `filled`
    count or a different obstacle rationale than the row the reader clicked.

    `claims` is the one thing they legitimately differ on. `claims_by_field` is
    48% of the list payload — 9.2 MB of 19 MB across 300 projects — for a table
    that renders one project at a time, so the list omits it and the page, which
    is about exactly one project, includes it.
    """
    payload = to_json_object(project, claims=claims)
    payload["iso"] = _iso_of(project)
    payload["nulls"] = _nulls(project)
    payload["unconfirmed_because"] = _unconfirmed_because(project)
    payload["filled"] = sum(1 for f in TRACKED_FIELDS if getattr(project, f, None) is not None)
    # Why this obstacle and not the other twenty-six. Computed by the module
    # that picked it, so the explanation cannot name a different risk than the
    # column holds.
    payload["blocker_rationale"] = blocker_rationale(project)
    _split_serving(payload)
    return payload


def build(session: Session, *, db_path: str, schema_version: int) -> dict[str, Any]:
    """The whole console payload for one request."""
    rows = fetch_projects(session)
    projects = [project_payload(project, claims=False) for project in rows]

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
        # The conversion the H200 column rests on. Sent rather than duplicated in
        # the front end: the page needs it to tell a derived count from a cited
        # one, and two copies of an assumption drift.
        "kwPerH200": _kw_per_h200(),
    }


__all__ = ["build"]

# --- what the console asks for, as against what the terminal UI asks for -----
#
# `build()` above is the whole database in one object. It is still exactly right
# for `tracker/tui/data.py`, which calls it in-process with the database beside
# it and pays no wire cost for anything it does not draw.
#
# The browser is the other case. Measured on a 26-project fixture, `build()` is
# 252 KB of JSON — 9.7 KB per project — of which two thirds is per-project detail
# no list view can show: every citation, every milestone, every tranche, every
# party. At 437 projects that is over 4 MB before the page can paint, and the
# number only grows. So the console gets three narrower answers instead: a light
# index of every project (below), a page of full-fidelity table rows
# (`webui/query.py`), and one project whole (`project_payload`).


#: What a table row does not carry, and why each is safe to leave out.
#:
#: Measured by reading every consumer, not by guessing: `events` is the project
#: page's timeline, `blocks`/`sections`/`serving`/`accounting` are its tranche
#: tab, `parties` its who-is-involved card, `basis` has no reader in the browser
#: at all (only `tracker/tui/data.py`), and `blocker_rationale` is the sentence
#: under the obstacle on the page. All of them arrive with `/api/project`.
#:
#: Anything added to a project payload lands in the table row by default. That is
#: deliberate: a new key the table cannot use should have to be named here, where
#: a reader can see the claim that nothing reads it.
LIST_OMITS: tuple[str, ...] = (
    "events",
    "blocks",
    "sections",
    "serving",
    "accounting",
    "parties",
    "basis",
    "blocker_rationale",
    "claims_by_field",
)

#: The four fields the table's obstacle filters and the map's markers read. The
#: rest of a risk row — its quote, its source, its dates — is read on the project
#: page and nowhere else.
RISK_LIST_FIELDS: tuple[str, ...] = ("status", "severity", "category", "summary")

#: Dropped from `standing` for the row. `timeline` is the full milestone history
#: the project page draws; the table draws `tracks`. `binding_blocker` has no
#: reader anywhere — it stays in the file export, where the guarantee is that
#: everything is in the file, and goes no further.
STANDING_OMITS: tuple[str, ...] = ("timeline", "binding_blocker")


def table_row(project: Any) -> dict[str, Any]:
    """One row of the projects table.

    **Built by subtraction from `project_payload`, never as a second builder.**
    The page and the row have to agree about what a project is — a row saying
    "9 of 12" beside a page saying "8 of 12" is a contradiction with no visible
    cause — so there is one definition and this removes from it. What it removes
    is `LIST_OMITS`, and `tests/test_webui.py` holds the subtraction to that
    exact set in both directions.
    """
    payload = project_payload(project, claims=False)
    for key in LIST_OMITS:
        payload.pop(key, None)
    standing = payload.get("standing")
    if isinstance(standing, dict):
        payload["standing"] = {k: v for k, v in standing.items() if k not in STANDING_OMITS}
    payload["risks"] = [
        {k: risk[k] for k in RISK_LIST_FIELDS if k in risk} for risk in payload.get("risks") or []
    ]
    # A count, not the array. 75.6 KB of that 252 KB fixture payload was
    # citations, and the table shows their number. The articles themselves are
    # the sources view's subject, and it asks for them separately.
    payload["n_sources"] = len(payload.pop("sources", None) or [])
    return payload


#: Every scalar the light index carries. Identity, location, the headline
#: numbers. No quote, no tier, no citation — a reader who wants those has
#: clicked a row by then.
INDEX_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "company",
    "customer",
    "city",
    "county",
    "state",
    "phase",
    "lat",
    "lon",
    "mw_planned",
    "mw_built",
    "investment_usd",
    "confidence",
    "blocker",
)


def index_rows(session: Session) -> list[dict[str, Any]]:
    """Every project, in the ~0.7 KB per row the maps and the pickers need.

    **This is what `window.DCTRACKER.projects` is, and it cannot be emptied.**
    `static/vendor/dc-map3d.js` reads that global directly rather than taking
    props, and both map components call `p.risks.some(...)` unguarded — a row
    without a `risks` array throws inside a custom element, which reaches the
    reader as a blank map and no error in the console. So every row here carries
    `risks`, even when it is empty, and the test suite pins that.

    Read with a bare select rather than `export.fetch_projects`, which eagerly
    loads sources, events, blocks and parties — the four things this deliberately
    does not send.
    """
    from tracker.models import Event, Project, Risk, Source

    projects = list(
        session.scalars(
            select(Project).order_by(Project.state, Project.company, Project.name, Project.id)
        )
    )
    risks: dict[int, list[dict[str, Any]]] = {}
    for risk in session.scalars(select(Risk)):
        risks.setdefault(risk.project_id, []).append(
            {field: getattr(risk, field) for field in RISK_LIST_FIELDS}
        )
    counts = dict(
        session.execute(
            select(Source.project_id, func.count(Source.id)).group_by(Source.project_id)
        ).all()
    )
    # Only the slipped dates, because only the slipped dates are drawn from a list
    # view: the capex breakdown's "delays" column names the sites whose expected
    # online date has moved. The rest of the history is the project page's
    # timeline.
    delays: dict[int, list[dict[str, Any]]] = {}
    for event in session.scalars(select(Event).where(Event.event_type == "delayed")):
        delays.setdefault(event.project_id, []).append(
            {
                "event_date": iso(event.event_date),
                "description": event.description,
            }
        )

    rows = []
    for project in projects:
        row: dict[str, Any] = {field: getattr(project, field) for field in INDEX_FIELDS}
        row["expected_online"] = iso(project.expected_online)
        row["first_announced"] = iso(project.first_announced)
        row["updated_at"] = iso(project.updated_at)
        row["risks"] = risks.get(project.id, [])
        row["delays"] = delays.get(project.id, [])
        row["n_sources"] = counts.get(project.id, 0)
        rows.append(row)
    return rows


def light(session: Session, *, schema_version: int) -> dict[str, Any]:
    """The shell payload: the light index, the aggregates, and the vocabularies.

    Everything here is either one number for the whole fleet or a list the front
    end needs before it can draw anything. What is *not* here, and why:

    - **per-project detail** — the table asks `/api/projects` for a page of 30
    - **`capex`** — 304 ms of `build()`'s 406 ms, for one view of six. It moved to
      `/api/capex`, which that view asks for when it opens.
    - **`db`**, the database's absolute path, which `build()` carries for the
      terminal interface. Nothing in the page read it, and on a published console
      it told every reader the host's directory layout, user name included.
    """
    from tracker.models import Project, Source

    rows = index_rows(session)
    citations = session.scalar(select(func.count(Source.id))) or 0
    queued = session.scalar(select(IngestUrl.id).where(IngestUrl.status == "discovered").limit(1))
    # `required.match` reads a project's identity fields off the ORM row, so the
    # bare select is enough — no relation it touches is loaded here.
    projects = list(session.scalars(select(Project)))

    return {
        "schema": "tracker/webui-1",
        "schema_version": schema_version,
        "version": __version__,
        "projects": rows,
        "totals": {
            "projects": len(rows),
            "citations": citations,
            "states": len({r["state"] for r in rows}),
            "mw_planned": sum(r["mw_planned"] or 0 for r in rows),
            "mw_cited_projects": sum(1 for r in rows if r["mw_planned"] is not None),
            "investment_usd": sum(r["investment_usd"] or 0 for r in rows),
            "queue_has_work": queued is not None,
        },
        "exposure": _risk_exposure(rows),
        "queue": _queue(session),
        "failed": _failed(session),
        "gaps": _gaps(session),
        "required": _required(session, projects),
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
        "kwPerH200": _kw_per_h200(),
    }


def capex(session: Session) -> dict[str, Any]:
    """The capex rollup on its own, for the one view that draws it.

    Its own route because it is 304 ms of a 406 ms payload — every visit to every
    other view was paying for a rollup it never shows.
    """
    return _capex(session)


#: What an article row carries. `excerpt` is deliberately absent from the row —
#: it is a paragraph per source and the list shows a URL — but it *is* searched,
#: because the browser's filter searched it and a search that silently stopped
#: looking somewhere is a search that lies.
ARTICLE_FIELDS: tuple[str, ...] = ("url", "source_type", "fields", "quotes")


def _article_row(source: Any, host: str) -> dict[str, Any]:
    row = {field: getattr(source, field, None) for field in ARTICLE_FIELDS}
    row["published_at"] = iso(source.published_at)
    row["fetched_at"] = iso(source.fetched_at)
    row["publisher"] = host
    row["projects"] = []
    return row


def articles(session: Session, *, q: str = "", host: str = "") -> dict[str, Any]:
    """The citations view's data, in the two sizes it actually needs.

    **Without `host`: the publisher list and nothing else** — a name, a count and
    a date per outlet. That is what the view paints at rest, and it is a few
    kilobytes for a database whose citations are a megabyte.

    **With `host`: that publisher's articles in full**, fetched when a reader
    expands the card. The alternative — shipping every article so one card can be
    opened — is the same mistake this whole change is undoing, one level down:
    measured on a 437-project fleet it is 1.25 MB to show what a click needs 40 KB
    of.

    `q` searches across publishers, so it has to reach the articles either way; it
    returns the matching ones already expanded, because a search result that has
    to be clicked open is not a result.

    **Grouped by `sources.host_of`, which is what the CLI prints.** The browser had
    its own rule — strip `www.`, keep the last two labels — and a comment claiming
    it matched the CLI's. It does not, for any publisher under a two-part suffix
    (`bbc.co.uk` became `co.uk`), so the measured record shown beside a host could
    be attached to the wrong host. One definition, and it is the one
    `confidence.registrable_domain` already owns.
    """
    from sqlalchemy.orm import selectinload

    from tracker.models import Project, Source
    from tracker.sources import host_of

    needle = q.strip().lower()
    wanted_host = host.strip().lower()
    stmt = select(Source).options(selectinload(Source.project)).order_by(Source.url, Source.id)
    # One article routinely cites several projects — 2,758 source rows over 1,928
    # distinct URLs — and the page wants the article once, carrying the list of
    # projects that rest on it.
    by_url: dict[str, dict[str, Any]] = {}
    counts: dict[str, dict[str, Any]] = {}
    for source in session.scalars(stmt):
        publisher = host_of(source.url)
        tally = counts.setdefault(publisher, {"host": publisher, "articles": 0, "last_at": None})
        seen = source.url in by_url
        if not seen:
            tally["articles"] += 1
            stamp = iso(source.published_at) or iso(source.fetched_at)
            if stamp and (tally["last_at"] is None or stamp > tally["last_at"]):
                tally["last_at"] = stamp

        if wanted_host and publisher.lower() != wanted_host:
            continue
        if needle and not (
            needle in publisher.lower()
            or needle in source.url.lower()
            or needle in (source.excerpt or "").lower()
        ):
            continue
        if not wanted_host and not needle:
            # The resting request wants counts, not content.
            continue

        entry = by_url.get(source.url)
        if entry is None:
            entry = _article_row(source, publisher)
            by_url[source.url] = entry
        project: Project | None = source.project
        if project is not None:
            entry["projects"].append(
                {"id": project.id, "name": f"{project.company} — {project.name}"}
            )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in by_url.values():
        grouped.setdefault(entry["publisher"], []).append(entry)
    for rows in grouped.values():
        rows.sort(key=lambda a: (a["published_at"] or a["fetched_at"] or "", a["url"]))

    publishers = [
        {**tally, "loaded": grouped.get(tally["host"])}
        for tally in sorted(counts.values(), key=lambda g: (-g["articles"], g["host"]))
        # A search narrows the publisher list too: an outlet with no matching
        # article is not a result with zero rows, it is not a result.
        if not (needle or wanted_host) or tally["host"] in grouped
    ]
    return {
        "publishers": publishers,
        "totals": {
            "publishers": len(counts),
            "articles": sum(t["articles"] for t in counts.values()),
            "matched": sum(len(rows) for rows in grouped.values())
            if (needle or wanted_host)
            else None,
        },
    }
