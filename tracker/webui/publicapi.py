"""The read-only public API, version 1: what each endpoint answers, and in what shape.

**Its own shapes, on purpose.** The console's `/api/*` routes answer whatever the
console's pages need this week, and change with them. A program calling this API
was written once and is not redeployed when a page is redesigned, so every
response here is built by this module from the export (`export.to_json_object`)
and the existing readers (`webui/query.py`, `capex`, `company`, `feed`,
`conditions`) into a shape documented in the reader docs
(`docs/en/api-reference.md`). Adding a key is allowed; renaming or removing one is
a new version.

**Reads as the key's account.** The dataset is the same for every account, so most
endpoints do not depend on who calls; `/v1/me` and `/v1/updates` do — the latter
is the account's own watchlist, as on its Updates page.

**Where it answers.** On the API hostname (`TRACKER_API_HOSTNAME`) the whole host is
the API, at `/v1/...`; on any other host it is under `/api/v1/...`, which is how it
is tried on a machine with no tunnel. `Handler._public_api` does the routing, the
key check and the rate limit; this module only answers.

Nothing here writes, spends a model call, or returns a key.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from tracker import apikeys

VERSION = "v1"

#: Requests per key per minute. Generous for a script paging through the table,
#: and low enough that a loop gone wrong costs the console nothing noticeable.
RATE_PER_MINUTE = 60

#: Most rows one page of `/v1/projects` may hold.
MAX_LIMIT = 200

#: The label a field's value carries, in the docs' words rather than the stored
#: tier names (`gaps.provenance`). See the reader docs, "Where a value came from".
LABELS: dict[str, str] = {
    "reported": "quoted",
    "derived": "derived",
    "unconfirmed": "unconfirmed",
    "inferred": "inferred",
    "defaulted": "default",
    "missing": "missing",
    "na": "not_applicable",
}

#: The fields a project summary carries, in this order.
SUMMARY_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "company",
    "customer",
    "city",
    "county",
    "state",
    "phase",
    "mw_planned",
    "mw_built",
    "investment_usd",
    "first_announced",
    "expected_online",
    "blocker",
    "h200_equivalent",
    "confidence",
    "lat",
    "lon",
    "updated_at",
    # Appended, as every addition to a v1 shape is: null, or why the campus reads
    # as late (`delays.assess`) — the same answer the console's chip gives.
    "suspected_delay",
)

#: Fields whose provenance a project's detail spells out.
PROVENANCE_FIELDS: tuple[str, ...] = (
    "name",
    "company",
    "customer",
    "city",
    "county",
    "state",
    "phase",
    "mw_planned",
    "mw_built",
    "investment_usd",
    "first_announced",
    "expected_online",
    "blocker",
    "h200_equivalent",
)

#: `/v1/projects` parameters, mapped onto the console table's own (`query.parse`),
#: so the API and the table cannot disagree about what a filter means.
LIST_PARAMS: dict[str, str] = {
    "q": "q",
    "state": "state",
    "phase": "phase",
    "min_confidence": "conf",
    "obstacle": "risk",
    "severity": "severity",
    "quoted_only": "quoted",
    "suspected_delay": "delayed",
    "sort": "sort",
    "order": "dir",
    "limit": "limit",
    "offset": "offset",
}

#: `/v1/conditions` parameters. `status` and `scope` take one value or several,
#: comma-separated.
CONDITION_PARAMS: frozenset[str] = frozenset({"state", "status", "scope", "limit", "offset"})

#: The fields a rule's summary carries, in this order: `conditions.as_json`'s, named
#: here so that a key the CLI adds or renames there does not move this contract.
CONDITION_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "kind",
    "need",
    "effect",
    "scope",
    "state",
    "county",
    "place",
    "region",
    "input",
    "applies_to",
    "min_mw",
    "status",
    "summary",
    "effective_on",
    "ends_on",
    "first_reported",
    "last_reported",
    "projects",
)

#: Rows on one page of `/v1/conditions` when `limit` is not given, as `/v1/projects`.
CONDITION_PAGE = 30

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,80}")


class ApiError(Exception):
    """An answer that is an error: an HTTP status, a stable code, and a sentence."""

    def __init__(
        self, status: int, code: str, message: str, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        #: Extra response headers, such as a 429's `Retry-After`.
        self.headers = headers or {}

    def as_json(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


# --- the rate limit ---------------------------------------------------------------


class RateLimiter:
    """A token bucket per key: `per_minute` requests, refilled continuously.

    In memory, per process. The console is one process, and a limit that resets
    when it restarts is the right trade for one that needs no table.
    """

    def __init__(
        self, per_minute: int = RATE_PER_MINUTE, clock: Callable[[], float] = time.monotonic
    ):
        self.per_minute = per_minute
        self._clock = clock
        self._buckets: dict[int, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key_id: int) -> tuple[bool, int, int]:
        """`(allowed, remaining, retry_after_seconds)` for one request by `key_id`."""
        rate = self.per_minute / 60.0
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(key_id, (float(self.per_minute), now))
            tokens = min(float(self.per_minute), tokens + (now - last) * rate)
            if tokens < 1:
                self._buckets[key_id] = (tokens, now)
                return False, 0, max(1, int((1 - tokens) / rate + 0.999))
            tokens -= 1
            self._buckets[key_id] = (tokens, now)
            return True, int(tokens), 0


# --- shapes -----------------------------------------------------------------------


def _summary(obj: dict[str, Any], prefix: str) -> dict[str, Any]:
    out = {name: obj.get(name) for name in SUMMARY_FIELDS}
    out["links"] = {"self": f"{prefix}/projects/{obj['id']}"}
    return out


def _fields(obj: dict[str, Any]) -> dict[str, Any]:
    prov = obj.get("prov") or {}
    fields: dict[str, Any] = {}
    for name in PROVENANCE_FIELDS:
        entry = prov.get(name) or {}
        value = obj.get(name)
        tier = entry.get("tier") or ("missing" if value is None else "reported")
        fields[name] = {
            "value": value,
            "label": LABELS.get(tier, tier),
            "quote": entry.get("quote") or None,
            "quote_is_exact": bool(entry.get("quote_is_exact")),
            "source_url": entry.get("source_url") or None,
        }
        # The claim's qualifiers (`vocab.CLAIM_AXES` and the date's precision), only
        # where they say something: what the figure covers, how firmly, and so on.
        axes = entry.get("axes") or {}
        for axis in ("scope", "bound", "modality", "as_of", "basis", "date_precision"):
            if axes.get(axis):
                fields[name][axis] = axes[axis]
        if obj.get(f"{name}_basis") and "basis" not in fields[name]:
            fields[name]["basis"] = obj[f"{name}_basis"]
    return fields


def project_detail(project: Any, prefix: str) -> dict[str, Any]:
    from tracker.export import to_json_object

    obj = to_json_object(project, claims=False)
    out = _summary(obj, prefix)
    out["country"] = obj.get("country")
    out["created_at"] = obj.get("created_at")
    out["last_verified_at"] = obj.get("last_verified_at")
    out["fields"] = _fields(obj)
    out["parties"] = [
        {
            "name": p["name"],
            "role": p["role"],
            "quote": p.get("quote"),
            "confirmed": p.get("unconfirmed") is None,
            # Appended (migration 0044): null, or when and why the role ended — a
            # former tenant, a sold site. A party with one is not a current buyer.
            "ended_on": p.get("ended_on"),
            "ended_reason": p.get("ended_reason"),
        }
        for p in obj.get("parties") or []
    ]
    out["tranches"] = [
        {
            "label": b["label"],
            "status": b["status"],
            "mw": b["mw"],
            "mw_counted": b["mw_counted"],
            "customer": b["customer"],
            "expected_online": b["expected_online"],
            "energized_on": b["energized_on"],
            "investment_usd": b["investment_usd"],
        }
        for b in obj.get("blocks") or []
    ]
    out["obstacles"] = [
        {
            "category": r["category"],
            "severity": r["severity"],
            "status": r["status"],
            "summary": r["summary"],
            "quote": r["quote"],
            "first_seen": r["first_seen"],
            "resolved_at": r["resolved_at"],
        }
        for r in obj.get("risks") or []
    ]
    out["milestones"] = [
        {
            "date": e["event_date"],
            "type": e["event_type"],
            "description": e["description"],
            "quote": e["quote"],
            "confirmed": e.get("unconfirmed") is None,
        }
        for e in obj.get("events") or []
    ]
    out["source_count"] = len(obj.get("sources") or [])
    out["links"]["sources"] = f"{prefix}/projects/{obj['id']}/sources"
    out["links"]["facts"] = f"{prefix}/projects/{obj['id']}/facts"
    return out


def project_sources(project: Any) -> list[dict[str, Any]]:
    from tracker.export import to_json_object

    obj = to_json_object(project, claims=False)
    return [
        {
            "url": s["url"],
            "type": s["source_type"],
            "published_at": s.get("published_at"),
            "fetched_at": s.get("fetched_at"),
            "excerpt": s.get("excerpt"),
            "quotes": s.get("quotes") or {},
        }
        for s in obj.get("sources") or []
    ]


def project_facts(session: Any, project: Any) -> list[dict[str, Any]]:
    """Every statement the sources make about one campus (migration 0045).

    One row each: what it is about, the field, the value, how far it can be trusted,
    the sentence and the citation, and whether it is the value the campus holds — the
    rows of `tracker export facts`, less the campus columns the path already names.
    """
    from tracker import facts

    rows = facts.rows_for(session, [project.id])
    return [
        {k: v for k, v in row.items() if k not in ("project_id", "company", "campus")}
        for row in rows
    ]


# --- endpoints --------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """What an endpoint is given: the path after the version, the query, the caller."""

    parts: tuple[str, ...]
    query: dict[str, list[str]]
    caller: apikeys.Caller | None
    prefix: str


#: Endpoints that answer without a key. Everything else needs one.
OPEN: frozenset[str] = frozenset({"", "health"})


def index(prefix: str, docs: str) -> dict[str, Any]:
    """`/v1` itself: what is here and where it is documented."""
    return {
        "api": "dc-tracker",
        "version": VERSION,
        "documentation": docs,
        "authentication": "Authorization: Bearer <key>. Make a key on your Account page.",
        "rate_limit": f"{RATE_PER_MINUTE} requests per minute per key",
        "endpoints": {
            f"GET {prefix}/health": "liveness and version (no key)",
            f"GET {prefix}/me": "the key and the account it reads as",
            f"GET {prefix}/projects": "campuses, filtered, sorted and paged",
            f"GET {prefix}/projects/{{id}}": "one campus, each field with its source",
            f"GET {prefix}/projects/{{id}}/sources": "every citation behind one campus",
            f"GET {prefix}/projects/{{id}}/facts": "every statement about one campus, with its tier",
            f"GET {prefix}/companies/{{slug}}": "one company's campuses and recent news",
            f"GET {prefix}/capex": "capacity and investment by buyer",
            f"GET {prefix}/updates": "recent news on the key's account's watchlist",
            f"GET {prefix}/conditions": "rules that apply to a place, filtered and paged",
            f"GET {prefix}/conditions/{{id}}": "one rule: its articles and the projects it reaches",
        },
    }


def _one(query: dict[str, list[str]], name: str) -> str:
    return (query.get(name) or [""])[0].strip()


def projects(session: Session, req: Request) -> dict[str, Any]:
    from tracker.export import fetch_projects
    from tracker.webui import query as table

    unknown = sorted(set(req.query) - set(LIST_PARAMS))
    if unknown:
        raise ApiError(400, "unknown_parameter", f"unknown parameter {unknown[0]!r}")
    translated = {LIST_PARAMS[k]: v for k, v in req.query.items()}
    try:
        filters, offset, limit = table.parse(translated)
    except table.BadQuery as exc:
        raise ApiError(400, "bad_parameter", str(exc)) from None
    limit = min(limit, MAX_LIMIT)
    matching = table.ids(session, filters)
    wanted = matching[offset : offset + limit]
    order = {pid: i for i, pid in enumerate(wanted)}
    rows = sorted(fetch_projects(session, only=wanted), key=lambda p: order[p.id]) if wanted else []
    from tracker.export import to_json_object

    data = [_summary(to_json_object(p, claims=False), req.prefix) for p in rows]
    nxt = offset + limit if offset + limit < len(matching) else None
    return {
        "total": len(matching),
        "offset": offset,
        "limit": limit,
        "next_offset": nxt,
        "data": data,
    }


def _project(session: Session, raw: str) -> Any:
    from tracker.models import Project

    if not raw.isdigit() or len(raw) > 18:
        raise ApiError(400, "bad_id", "a project id is a whole number")
    project = session.get(Project, int(raw))
    if project is None:
        raise ApiError(
            404,
            "not_found",
            f"no project {raw}; it may have been merged into another as a duplicate",
        )
    return project


def company(session: Session, slug: str, prefix: str, *, tally: Any = None) -> dict[str, Any]:
    """`tally` is the console's cached capex rollup (`capex.tally`); see `company.profile`."""
    from tracker import company as company_mod

    if not _SLUG.fullmatch(slug):
        raise ApiError(400, "bad_slug", "a company slug is lowercase letters, digits and hyphens")
    profile = company_mod.profile(session, slug, tally=tally)
    if profile is None:
        raise ApiError(404, "not_found", f"no campus is built or occupied by {slug!r}")
    return {
        "slug": profile["slug"],
        "name": profile["name"],
        # The company as an entity (migration 0043): its kind, whose subsidiary it
        # is and which are its (each `{name, slug}`), and the other ways sources
        # write its name. Additive.
        "kind": profile["kind"],
        "parent": profile["parent"],
        "subsidiaries": profile["subsidiaries"],
        "also_written": profile["also_written"],
        "website": profile["domain"],
        "totals": profile["totals"],
        # The campuses it builds, whole, whoever buys them, beside `totals` (which
        # credits each campus to its buyer, as Capex does): `{campuses, mw_planned,
        # mw_built}`. Additive.
        "builds": {k: profile["builds"][k] for k in ("campuses", "mw_planned", "mw_built")},
        "states": [{"state": s, "projects": n} for s, n in profile["states"]],
        "phases": [{"phase": p, "projects": n} for p, n in profile["phases"]],
        "projects": [
            {**row, "links": {"self": f"{prefix}/projects/{row['id']}"}}
            for row in profile["projects"]
        ],
        "updates": profile["updates"],
        "suspected_delays": profile["suspected_delays"],
        # Companies its campuses' sources named for the first time in the last 30
        # days — suppliers first — as on the company page (`parties.newly_named`).
        # Additive.
        "new_parties": profile["new_parties"],
    }


def capex(payload: dict[str, Any]) -> dict[str, Any]:
    """The console's capex rollup, reduced to its rows and what qualifies them."""
    keep = (
        "customer",
        "projects",
        "self_built",
        "undisclosed",
        "mw_planned",
        "mw_built",
        "investment_usd",
        "investment_unquoted_usd",
        "investment_excluded_usd",
        "investment_out_of_scope_usd",
        "mw_by_year",
        "mw_at_risk",
        "projects_at_risk",
        "slipped",
        "duplicate_rows_skipped",
        "mw_duplicate_skipped",
        "project_ids",
    )
    return {
        "as_of_year": payload.get("as_of_year"),
        "note": "Every figure is a floor: capacity no source states counts as zero. "
        "Paused and cancelled campuses are left out. Suspected duplicate rows are "
        "counted once and disclosed in duplicate_rows_skipped.",
        "unattributed_label": payload.get("unattributed"),
        "buyers": [{k: row.get(k) for k in keep} for row in payload.get("positions") or []],
        "coverage": payload.get("coverage"),
    }


def updates(session: Session, req: Request) -> dict[str, Any]:
    from tracker import feed

    raw = _one(req.query, "days") or "7"
    if not raw.isdigit() or not 1 <= int(raw) <= feed.REPORT_WINDOW_DAYS:
        raise ApiError(
            400, "bad_parameter", f"days must be a whole number from 1 to {feed.REPORT_WINDOW_DAYS}"
        )
    account_id = req.caller.account_id if req.caller else None
    digest = feed.digest(session, days=int(raw), account_id=account_id)
    return {
        "days": int(raw),
        "watching_everything": digest.watching_everything,
        "projects_watched": digest.projects_watched,
        "last_crawl": digest.last_crawl.isoformat() if digest.last_crawl else None,
        "data": [s.as_json() for s in digest.signals],
        "unconfirmed": [s.as_json() for s in digest.held],
    }


def _values(query: dict[str, list[str]], name: str, allowed: tuple[str, ...]) -> set[str]:
    """`?status=pledged,in_force` as a set, or a 400 naming the value not allowed."""
    raw = _one(query, name)
    wanted = {v.strip().lower() for v in raw.split(",") if v.strip()}
    bad = sorted(wanted - set(allowed))
    if bad:
        raise ApiError(
            400, "bad_parameter", f"{name} {bad[0]!r} is not one of {', '.join(allowed)}"
        )
    return wanted


def _whole(query: dict[str, list[str]], name: str, default: int) -> int:
    raw = _one(query, name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ApiError(
            400, "bad_parameter", f"{name} must be a whole number, not {raw!r}"
        ) from None


def condition_summary(condition: Any, prefix: str, *, reach: int) -> dict[str, Any]:
    """One rule as `/v1/conditions` lists it.

    `where` is the place in words ("Piqua, OH", "ERCOT"), and `market_context` is
    true for a rule of the whole country or a whole-industry input, which reaches
    no project by design (`conditions.PLACED`): `projects` is 0 for those because
    flagging every campus says nothing, not because none is affected.
    """
    from tracker import conditions

    obj = conditions.as_json(condition, reach=reach)
    out = {name: obj.get(name) for name in CONDITION_FIELDS}
    out["where"] = conditions.where_words(condition)
    out["market_context"] = condition.scope not in conditions.PLACED
    out["links"] = {"self": f"{prefix}/conditions/{condition.id}"}
    return out


def conditions_list(session: Session, req: Request) -> dict[str, Any]:
    """Rules that apply to a place, newest report first, filtered and paged like
    `/v1/projects`. Every status by default, lifted and voted-down ones included:
    a program keeping its own copy needs to hear that a rule ended."""
    from tracker import conditions
    from tracker.vocab import CONDITION_SCOPES, CONDITION_STATUSES

    unknown = sorted(set(req.query) - CONDITION_PARAMS)
    if unknown:
        raise ApiError(400, "unknown_parameter", f"unknown parameter {unknown[0]!r}")
    state = _one(req.query, "state").upper()
    if state and not re.fullmatch(r"[A-Z]{2}", state):
        raise ApiError(400, "bad_parameter", "state is a two-letter code, such as TX")
    statuses = _values(req.query, "status", CONDITION_STATUSES)
    scopes = _values(req.query, "scope", CONDITION_SCOPES)
    offset = _whole(req.query, "offset", 0)
    if offset < 0:
        raise ApiError(400, "bad_parameter", "offset cannot be negative")
    limit = _whole(req.query, "limit", CONDITION_PAGE)
    if limit < 1:
        raise ApiError(400, "bad_parameter", "limit must be at least 1")
    limit = min(limit, MAX_LIMIT)

    matching = [
        c
        for c in conditions.listing(session, state=state or None, cleared=True)
        if (not statuses or c.status in statuses) and (not scopes or c.scope in scopes)
    ]
    counts = conditions.reach_counts(session)
    page = matching[offset : offset + limit]
    nxt = offset + limit if offset + limit < len(matching) else None
    return {
        "total": len(matching),
        "offset": offset,
        "limit": limit,
        "next_offset": nxt,
        "data": [condition_summary(c, req.prefix, reach=counts.get(c.id, 0)) for c in page],
    }


def condition_detail(session: Session, raw: str, prefix: str) -> dict[str, Any]:
    """One rule: its summary, each article that stated it with the article's own
    sentences, and every project it reaches with how sure the match is and why."""
    from sqlalchemy import select

    from tracker import conditions
    from tracker.models import Condition, Exposure, Project

    if not raw.isdigit() or len(raw) > 18:
        raise ApiError(400, "bad_id", "a condition id is a whole number")
    condition = session.get(Condition, int(raw))
    if condition is None:
        raise ApiError(
            404, "not_found", f"no condition {raw}; it may have been dropped as read wrongly"
        )
    reached = session.execute(
        select(Exposure, Project)
        .join(Project, Project.id == Exposure.project_id)
        .where(Exposure.condition_id == condition.id)
        .order_by(Project.state, Project.id)
    ).all()
    out = condition_summary(condition, prefix, reach=len(reached))
    sources = sorted(
        condition.sources, key=lambda s: (s.published_at or s.read_at, s.id or 0), reverse=True
    )
    out["articles"] = [
        {
            "url": s.url,
            "title": s.title,
            "published_at": s.published_at.isoformat() if s.published_at else None,
            "status": s.status,
            "summary": s.summary,
            "quotes": conditions.quotes_of(s),
        }
        for s in sources
    ]
    out["affected_projects"] = [
        {
            "project_id": project.id,
            "name": project.name,
            "company": project.company,
            "city": project.city,
            "county": project.county,
            "state": project.state,
            "phase": project.phase,
            "fit": exposure.fit,
            "why": exposure.why,
            "first_seen": exposure.first_seen.isoformat() if exposure.first_seen else None,
            "links": {"self": f"{prefix}/projects/{project.id}"},
        }
        for exposure, project in reached
    ]
    return out


def me(session: Session, req: Request) -> dict[str, Any]:
    from tracker import accounts
    from tracker.models import ApiKey

    caller = req.caller
    row = session.get(ApiKey, caller.key_id)
    account = accounts.by_id(session, caller.account_id)
    return {
        "key": apikeys.as_json(row),
        "account": {"email": account.email, "name": account.name},
        "rate_limit_per_minute": RATE_PER_MINUTE,
    }


__all__ = [
    "CONDITION_FIELDS",
    "CONDITION_PARAMS",
    "LABELS",
    "MAX_LIMIT",
    "OPEN",
    "RATE_PER_MINUTE",
    "VERSION",
    "ApiError",
    "RateLimiter",
    "Request",
    "capex",
    "company",
    "condition_detail",
    "condition_summary",
    "conditions_list",
    "index",
    "me",
    "project_detail",
    "project_facts",
    "project_sources",
    "projects",
    "updates",
]
