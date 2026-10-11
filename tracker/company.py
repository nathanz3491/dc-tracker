"""One company's page: every campus it builds or occupies, and what moved on them.

The console had a page per project and none per company, so a watchlist entry like
"Google" — twenty-five campuses — led nowhere. This is the read behind
`/companies/<slug>`.

**A company is matched the way the watchlist matches it**, through
`watchlist._project_keys`: the operator, the customer, and a tranche's customer,
each normalised by `dedup.company_key`. The page and the watch on the same name
therefore cover the same campuses — a "Google" page that disagreed with a "Google"
watch would be two answers to one question — and each campus says which way it
matched, so a builder can be told from a tenant.

**The slug is the key with hyphens**: `compass datacenters` → `compass-datacenters`.
Letters, digits and hyphens only, because the server puts it into the page shell
and that restriction is the whole defence against script injection there.

Nothing here is stored; it is a reading of rows that already carry their dates and
citations, like `feed` and `watchfor`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tracker import company_logo, feed, watchlist
from tracker.dedup import company_slug as slug
from tracker.models import Project, Source
from tracker.vocab import OPEN_RISK_STATUS

#: A slug as the URL and the page shell carry it. See the module docstring.
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,80}")


def profile(session: Session, wanted: str) -> dict[str, Any] | None:
    """Everything the company page shows, or None when no campus matches."""
    if not SLUG.fullmatch(wanted or ""):
        return None
    projects = session.scalars(
        select(Project)
        .options(
            selectinload(Project.blocks),
            selectinload(Project.events),
            selectinload(Project.risks),
        )
        .order_by(Project.id.asc())
    ).all()

    matched: list[tuple[Project, str]] = []
    names: Counter[str] = Counter()
    keys: Counter[str] = Counter()
    for project in projects:
        for key, via in watchlist._project_keys(project):
            if key and slug(key) == wanted:
                matched.append((project, via))
                keys[key] += 1
                raw = project.company if via == watchlist.VIA_OPERATOR else project.customer
                if raw and via != watchlist.VIA_BLOCK:
                    names[raw.strip()] += 1
                break
    if not matched:
        return None

    key = keys.most_common(1)[0][0]
    name = names.most_common(1)[0][0] if names else key.title()
    ids = [p.id for p, _ in matched]
    sources = {
        row.id: row
        for row in session.scalars(
            select(Source).where(Source.project_id.in_(ids)).order_by(Source.id.asc())
        ).all()
    }
    since = dt.datetime.combine(
        dt.date.today() - dt.timedelta(days=feed.REPORT_WINDOW_DAYS), dt.time.min
    )
    signals = [
        s
        for project, via in matched
        for s in feed.signals_for(project, since=since, sources=sources, entry=name, via=via)
        if s.confirmed
    ]
    rows = []
    for project, via in matched:
        open_risks = [r for r in project.risks if r.status == OPEN_RISK_STATUS]
        rows.append(
            {
                "id": project.id,
                "name": project.name,
                "company": project.company,
                "customer": project.customer,
                "city": project.city,
                "county": project.county,
                "state": project.state,
                "phase": project.phase,
                "mw_planned": project.mw_planned,
                "mw_built": project.mw_built,
                "investment_usd": project.investment_usd,
                "expected_online": (
                    project.expected_online.isoformat() if project.expected_online else None
                ),
                "confidence": project.confidence,
                "via": via,
                "open_obstacles": len(open_risks),
            }
        )
    rows.sort(key=lambda r: (-(r["mw_planned"] or 0), r["name"].lower()))
    # Its own website comes from the campuses it builds, where its newsroom is
    # likeliest to be cited; a tenant's page cites the landlord's site instead.
    own = {p.id for p, via in matched if via == watchlist.VIA_OPERATOR}
    urls = [s.url for s in sources.values() if s.project_id in own] or [
        s.url for s in sources.values()
    ]
    return {
        "slug": wanted,
        "key": key,
        "name": name,
        "domain": company_logo.website(key, urls),
        "states": Counter(r["state"] for r in rows if r["state"]).most_common(),
        "phases": Counter(r["phase"] for r in rows if r["phase"]).most_common(),
        "projects": rows,
        "totals": {
            "projects": len(rows),
            "as_operator": sum(1 for r in rows if r["via"] == watchlist.VIA_OPERATOR),
            "as_tenant": sum(1 for r in rows if r["via"] != watchlist.VIA_OPERATOR),
            "mw_planned": sum(r["mw_planned"] or 0 for r in rows),
            "mw_built": sum(r["mw_built"] or 0 for r in rows),
            "open_obstacles": sum(r["open_obstacles"] for r in rows),
        },
        "updates": [s.as_json() for s in feed.rank(feed.fold(signals))][:20],
    }


__all__ = ["SLUG", "profile"]
