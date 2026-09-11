"""Search, filter, sort and page the projects table, on the server.

The console used to download every project and do all four in the browser. That
works until it doesn't: the whole payload has to land before the first row can be
drawn, and it grows with the database. This module is the other half of the fix —
the table asks a question and gets back thirty rows and a count.

**Two steps, and the order is the point.** The filtered, sorted *id list* is
resolved first; only the ids on the requested page are then hydrated into rows.
That is what makes the count honest — `total` is the length of the id list, not
of what was sent — and it is what keeps a filter this module cannot express in
SQL from silently paging wrong. Filtering after `LIMIT` would page a filtered
view of an unfiltered slice, which looks like data loss.

**The browser's answers must not change.** Every predicate here mirrors one the
front end applied to the same rows last week, and a reader who gets different
rows for the same query has caught us lying. This module now owns the search
rule outright — `matchesProject` in `static/app.js` was its only other definition
and is deleted, because two of them would drift — so the behaviours it inherited
are pinned by name in `tests/test_webui.py`: every word must appear somewhere in
the row, `#42` is the id and nothing else, and a bare `42` stays a substring
match because it is also a capacity, a year, and part of a name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, cast, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.types import String

from tracker.models import Project, Risk
from tracker.vocab import TRACKED_FIELDS

log = logging.getLogger(__name__)

#: Rows per request. Thirty fills a laptop's first screen with a little to
#: spare, which is what a scroll-to-load wants: enough that the sentinel is
#: below the fold, few enough that the first page is cheap.
PAGE = 30

#: A caller may not ask for an unbounded page. The cap is not about the server —
#: it is that `limit=100000` is how a client accidentally reinstates the whole
#: download this module exists to remove.
MAX_PAGE = 200

#: Columns the table may sort by: its twelve tracked fields, the audit columns
#: behind the "all fields" switch, and the id. Every one is a real column on
#: `projects`, so sorting is `ORDER BY` and not a Python pass over everything.
#:
#: A whitelist rather than a `getattr`, because `?sort=` arrives from a URL. An
#: unknown key is a 400 and not a silent fallback to a default ordering: a table
#: that says "sorted by investment" while showing something else is worse than an
#: error.
SORTABLE: frozenset[str] = frozenset(
    {
        "id",
        *TRACKED_FIELDS,
        "h200_equivalent",
        "county",
        "lat",
        "lon",
        "confidence",
        "last_verified_at",
        "updated_at",
        "created_at",
    }
)

#: Searched by the free-text box, in the order `matchesProject` joins them. The
#: id is included as text, so `42` finds project 42 as well as any row with 42 in
#: it — the front end's behaviour, deliberately kept.
SEARCH_FIELDS: tuple[str, ...] = (
    "name",
    "company",
    "customer",
    "city",
    "county",
    "state",
    "blocker",
)

#: Tiers that fail "Quoted only". A value with no provenance row at all reads as
#: `reported`, matching the front end's `provOf(p, key)?.tier || "reported"`, and
#: an *empty* field is not a failure — most dashes in the table are correct
#: answers, not unquoted ones.
UNQUOTED_TIERS: frozenset[str] = frozenset({"unconfirmed", "inferred", "defaulted"})


@dataclass(frozen=True)
class Filters:
    """One table query. Every field mirrors a control in the filter card."""

    q: str = ""
    state: str = ""
    phase: str = ""
    conf: int | None = None
    risk: str = ""
    severity: str = ""
    quoted: bool = False
    sort: str = "confidence"
    direction: str = "desc"

    @property
    def needs_provenance(self) -> bool:
        return self.quoted


class BadQuery(ValueError):
    """A query string the table could not have produced. Answered with a 400."""


def parse(query: dict[str, list[str]]) -> tuple[Filters, int, int]:
    """Read `?q=&state=…&offset=&limit=` into a query, or refuse it.

    Refuses rather than repairs. A clamped `limit` is a kindness — the caller
    still gets rows — but a misspelled `sort` or a negative `offset` means the
    caller and this module disagree about what was asked, and the honest answer
    to that is an error.
    """

    def one(name: str, default: str = "") -> str:
        return (query.get(name) or [default])[0].strip()

    def number(name: str, default: int) -> int:
        raw = one(name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            raise BadQuery(f"{name} must be a whole number, not {raw!r}") from None

    sort = one("sort", "confidence") or "confidence"
    if sort not in SORTABLE:
        raise BadQuery(f"cannot sort by {sort!r}")
    direction = (one("dir", "desc") or "desc").lower()
    if direction not in {"asc", "desc"}:
        raise BadQuery(f"dir must be asc or desc, not {direction!r}")

    conf_raw = one("conf")
    conf = None
    if conf_raw:
        try:
            conf = int(conf_raw)
        except ValueError:
            raise BadQuery(f"conf must be a whole number, not {conf_raw!r}") from None

    offset = number("offset", 0)
    if offset < 0:
        raise BadQuery("offset cannot be negative")
    limit = number("limit", PAGE)
    if limit < 1:
        raise BadQuery("limit must be at least 1")
    limit = min(limit, MAX_PAGE)

    filters = Filters(
        q=one("q"),
        state=one("state"),
        phase=one("phase"),
        conf=conf,
        risk=one("risk"),
        severity=one("severity"),
        quoted=one("quoted").lower() in {"1", "true", "yes"},
        sort=sort,
        direction=direction,
    )
    return filters, offset, limit


def _like(word: str) -> str:
    """A LIKE pattern matching `word` anywhere, with its wildcards defanged.

    A reader searching for `50%` means the two characters, not "anything". Left
    unescaped, `%` and `_` in a query turn a search into a much wider one and the
    table quietly returns rows that do not contain what was typed.
    """
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _search(stmt: Select[Any], q: str) -> Select[Any]:
    """`matchesProject`, in SQL.

    Two behaviours worth naming because they are not accidents: `#42` is an exact
    id and nothing else, so a reader who knows the id can reach it past a name
    that happens to contain the digits; and every word must appear *somewhere* in
    the row, so "meta ohio" narrows rather than widens.
    """
    if q.startswith("#"):
        wanted = q[1:].strip()
        if not wanted.isdigit():
            # `#` with no number matches nothing, exactly as the browser's
            # `String(p.id) === q.slice(1)` did. Not "everything".
            return stmt.where(Project.id.is_(None))
        return stmt.where(Project.id == int(wanted))

    haystack = [cast(Project.id, String), *(getattr(Project, f) for f in SEARCH_FIELDS)]
    for word in q.lower().split():
        pattern = _like(word)
        stmt = stmt.where(
            or_(*(func.lower(column).like(pattern, escape="\\") for column in haystack))
        )
    return stmt


def _where(stmt: Select[Any], filters: Filters) -> Select[Any]:
    if filters.q:
        stmt = _search(stmt, filters.q)
    if filters.state:
        stmt = stmt.where(Project.state == filters.state)
    if filters.phase:
        stmt = stmt.where(Project.phase == filters.phase)
    if filters.conf is not None:
        stmt = stmt.where(Project.confidence >= filters.conf)
    # Both obstacle filters ask about an *open* risk, which is what the table's
    # two selects say on the tin. A resolved permitting fight is history, not a
    # reason to list the project under permitting risk today.
    if filters.risk:
        stmt = stmt.where(
            select(Risk.id)
            .where(
                Risk.project_id == Project.id,
                Risk.status == "open",
                Risk.category == filters.risk,
            )
            .exists()
        )
    if filters.severity:
        stmt = stmt.where(
            select(Risk.id)
            .where(
                Risk.project_id == Project.id,
                Risk.status == "open",
                Risk.severity == filters.severity,
            )
            .exists()
        )
    return stmt


def _order(stmt: Select[Any], filters: Filters) -> Select[Any]:
    """Sort, with empties last in both directions and the id as a tiebreak.

    Nulls last regardless of direction is the front end's rule and the right one:
    a column sorted ascending should open with the smallest figure somebody
    cited, not with sixty dashes. `NULLS LAST` is avoided in favour of a boolean
    key, which every SQLite this runs on understands.

    The id tiebreak is not cosmetic — it is what makes paging safe. Two rows with
    equal confidence in an unspecified order can both appear on page one and page
    two, or neither.
    """
    column = getattr(Project, filters.sort)
    primary = column.desc() if filters.direction == "desc" else column.asc()
    return stmt.order_by(column.is_(None), primary, Project.id.asc())


def ids(session: Session, filters: Filters) -> list[int]:
    """Every project matching `filters`, in the requested order."""
    stmt = _order(_where(select(Project.id), filters), filters)
    found = list(session.scalars(stmt))
    if filters.needs_provenance:
        allowed = _quoted_ids(session)
        found = [pid for pid in found if pid in allowed]
    return found


def page(
    session: Session, filters: Filters, *, offset: int = 0, limit: int = PAGE
) -> tuple[int, list[dict[str, Any]]]:
    """`(total, rows)` — the count for the whole filter, and one page of it."""
    from tracker.export import fetch_projects
    from tracker.webui.dataset import table_row

    matching = ids(session, filters)
    wanted = matching[offset : offset + limit]
    if not wanted:
        return len(matching), []

    # Hydrated in the order the page asked for, not the order the database
    # returned them in: `fetch_projects` orders by content for the export's sake,
    # and a page sorted by investment must arrive sorted by investment.
    order = {pid: i for i, pid in enumerate(wanted)}
    rows = sorted(fetch_projects(session, only=wanted), key=lambda p: order[p.id])
    return len(matching), [table_row(project) for project in rows]


# --- "Quoted only" ----------------------------------------------------------
#
# The one filter that cannot be a WHERE clause. Provenance is not stored: a
# field's tier is derived from the project's sources every time it is asked for
# (`gaps.provenance`), so there is no column to compare and no index to use.
#
# So it is a set of ids, computed by hydrating every project and asking `gaps` —
# around 0.9s for 437 projects, which is far too slow to pay per keystroke and
# perfectly affordable once. It is computed only when the switch is on, which is
# not the resting state, and memoised against a fingerprint of the data rather
# than a clock: the console refetches after every run, and a cache that expired
# on a timer would sometimes answer with the tiers of a database two ingests ago.


@dataclass
class _Memo:
    fingerprint: tuple[int, str | None] | None = None
    allowed: frozenset[int] = field(default_factory=frozenset)


_memo = _Memo()


def _fingerprint(session: Session) -> tuple[int, str | None]:
    """Cheap evidence that the data has not changed: how many rows, and when.

    Sources rather than projects, because a project's tiers change when a
    citation arrives even though its own row may not be touched.
    """
    from tracker.models import Source

    count = session.scalar(select(func.count(Source.id))) or 0
    latest = session.scalar(select(func.max(Project.updated_at)))
    return int(count), str(latest) if latest is not None else None


def _quoted_ids(session: Session) -> frozenset[int]:
    """Projects where every populated tracked field rests on a quote or a lookup."""
    from tracker.export import fetch_projects
    from tracker.gaps import basis as tier_of

    stamp = _fingerprint(session)
    if _memo.fingerprint == stamp:
        return _memo.allowed

    allowed = set()
    for project in fetch_projects(session):
        for name in TRACKED_FIELDS:
            value = getattr(project, name, None)
            if value is None or value == "":
                continue
            if (tier_of(project, name) or "reported") in UNQUOTED_TIERS:
                break
        else:
            allowed.add(project.id)
    _memo.fingerprint = stamp
    _memo.allowed = frozenset(allowed)
    log.debug("quoted-only set rebuilt: %d of the fleet", len(allowed))
    return _memo.allowed
