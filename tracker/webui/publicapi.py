"""The read-only public API, version 1: what each endpoint answers, and in what shape.

**Its own shapes, on purpose.** The console's `/api/*` routes answer whatever the
console's pages need this week, and change with them. A program calling this API
was written once and is not redeployed when a page is redesigned, so every
response here is built by this module from the export (`export.to_json_object`)
and the existing readers (`webui/query.py`, `capex`, `company`, `feed`) into a
shape documented in the reader docs (`docs/en/api-reference.md`). Adding a key is
allowed; renaming or removing one is a new version.

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
    "sort": "sort",
    "order": "dir",
    "limit": "limit",
    "offset": "offset",
}

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
            f"GET {prefix}/companies/{{slug}}": "one company's campuses and recent news",
            f"GET {prefix}/capex": "capacity and investment by buyer",
            f"GET {prefix}/updates": "recent news on the key's account's watchlist",
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


def company(session: Session, slug: str, prefix: str) -> dict[str, Any]:
    from tracker import company as company_mod

    if not _SLUG.fullmatch(slug):
        raise ApiError(400, "bad_slug", "a company slug is lowercase letters, digits and hyphens")
    profile = company_mod.profile(session, slug)
    if profile is None:
        raise ApiError(404, "not_found", f"no campus is built or occupied by {slug!r}")
    return {
        "slug": profile["slug"],
        "name": profile["name"],
        "website": profile["domain"],
        "totals": profile["totals"],
        "states": [{"state": s, "projects": n} for s, n in profile["states"]],
        "phases": [{"phase": p, "projects": n} for p, n in profile["phases"]],
        "projects": [
            {**row, "links": {"self": f"{prefix}/projects/{row['id']}"}}
            for row in profile["projects"]
        ],
        "updates": profile["updates"],
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
    "index",
    "me",
    "project_detail",
    "project_sources",
    "projects",
    "updates",
]
