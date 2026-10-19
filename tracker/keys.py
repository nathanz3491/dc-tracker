"""Every key a campus can be reached by.

`project.dedup_key` is written once, at insert, and never rewritten — re-keying a
row would collide with whatever already holds the new key, so the column is the
identity the row was *born* with (see `parties`, "churn is worse than staleness").
Two things make a row's identity drift away from that birth key, and both cost the
nightly loop real duplicates:

* **The row learns where it is.** A campus first filed under its county gains a
  town from a later article (identity fields are fill-only, so they only ever
  fill). Measured on 2026-10-19: 21 rows held a key that no longer described them —
  Hyperion (#10) keyed `meta|county:richland|LA` while its town is Rayville — so an
  article naming the town computed a key nothing answered to, and paid for an
  identity check or created a twin.
* **The rules learn a spelling.** When `dedup.company_key` learns that RagingWire is
  NTT, every row born under the old spelling keeps its old key, while every article
  arriving under either spelling now computes the new one. The same night 169 rows'
  natural keys differed from their stored ones once the roster's aliases joined the
  key.

So instead of rewriting the birth key, this records each row's *current* natural
key as a `project_alias` pointing at it — the same table a merge writes, read by
`upsert_record` right after an exact-key miss. A row stays reachable by the key it
was born with and by the key it would be born with today.

**What it never does is choose between two rows.** A natural key another row holds
outright is left to that row: the lookup already answers it, and the pair is a
question for the duplicate passes (`capex` raises it as `key` evidence). A natural
key several rows share and none holds goes to the one a merge would keep — most
citations, then the lower id (`dupresolve.survivor` uses the same order) — because
routing an article to one of them is the identity rule this table has always
applied ("name is display text; location is identity"), and minting a fourth NTT
row in Sacramento is not.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.dedup import company_key, dedup_key
from tracker.models import Project, ProjectAlias, Source

log = logging.getLogger(__name__)

#: `project_alias.decided_by` for an alias this module writes. Distinct from a
#: merge's decider, so a regression set can tell a remembered spelling from a fold.
DECIDED_BY: Final = "identity"

#: `decided_by` for a key the identity arbiter routed onto a row at insert.
ARBITER: Final = "arbiter"


def natural_key(project: Project) -> str:
    """The key this row would be filed under if it arrived today."""
    return dedup_key(project.company, project.city, project.county, project.state)


def renormalise(key: str) -> str:
    """An older key, re-read under today's company rules.

    A key is `company|kind:locality|STATE`, and its company part is an earlier
    `company_key` output, so passing it through `company_key` again applies what the
    rules have learned since — "ragingwire" becomes "ntt". The locality and state are
    already normalised and are returned untouched. Anything not shaped like a key is
    returned as it is.
    """
    parts = key.split("|", 2)
    if len(parts) != 3:
        return key
    company, locality, state = parts
    return f"{company_key(company) or company}|{locality}|{state}"


@dataclass
class KeyReport:
    """What one `sync` found and wrote."""

    #: (key, project id) for every alias written.
    added: list[tuple[str, int]] = field(default_factory=list)
    #: natural key -> rows that share it while none holds it as its own key.
    shared: dict[str, list[int]] = field(default_factory=dict)
    #: natural key -> (row, the other row that already holds that key).
    held_elsewhere: list[tuple[str, int, int]] = field(default_factory=list)


def _citations(session: Session) -> dict[int, int]:
    rows = session.execute(
        select(Source.project_id, func.count(Source.id)).group_by(Source.project_id)
    )
    return dict(rows.tuples().all())


def sync(session: Session) -> KeyReport:
    """Give every row an alias for its natural key, and every alias its renormalised key.

    Idempotent: a second run writes nothing. Cheap: one pass over the projects and
    one over the aliases, no model, no network. Run by `tracker init` on every
    deploy and by `backfill derive` every night, so a change to the company rules
    reaches every stored row before the next article does.
    """
    report = KeyReport()
    projects = list(session.scalars(select(Project)))
    stored = {p.dedup_key: p.id for p in projects}
    existing = list(session.scalars(select(ProjectAlias)))
    taken = set(stored) | {a.from_dedup_key for a in existing}
    counts = _citations(session)

    def add(key: str, project_id: int) -> None:
        session.add(
            ProjectAlias(from_dedup_key=key, to_project_id=project_id, decided_by=DECIDED_BY)
        )
        taken.add(key)
        report.added.append((key, project_id))

    by_natural: dict[str, list[Project]] = defaultdict(list)
    for project in projects:
        key = natural_key(project)
        if key and key != project.dedup_key:
            by_natural[key].append(project)

    for key, rows in sorted(by_natural.items()):
        if key in stored:
            report.held_elsewhere.extend((key, row.id, stored[key]) for row in rows)
            continue
        if key in taken:
            continue
        if len(rows) > 1:
            report.shared[key] = sorted(row.id for row in rows)
        add(key, max(rows, key=lambda p: (counts.get(p.id, 0), -p.id)).id)

    # Aliases a merge wrote under older rules: "ragingwire|city:sacramento|CA" still
    # reaches the survivor, and so must the "ntt|…" an article computes today.
    for alias in existing:
        key = renormalise(alias.from_dedup_key)
        if key not in taken:
            add(key, alias.to_project_id)

    if report.added:
        session.flush()
        log.info("remembered %d key(s) campuses can now be reached by", len(report.added))
    return report


def remember(
    session: Session, project: Project, *, key: str | None = None, by: str = DECIDED_BY
) -> bool:
    """Record `key` (default: the row's natural key) as reaching `project`, if free.

    Free means no row holds it as its own key and no alias claims it yet: an
    existing answer is never overwritten here, because this records what a row is
    *also* called, not a decision about two rows. Returns True when written.
    """
    key = key or natural_key(project)
    if not key or key == project.dedup_key:
        return False
    if session.scalar(select(Project.id).where(Project.dedup_key == key)) is not None:
        return False
    if (
        session.scalar(select(ProjectAlias.id).where(ProjectAlias.from_dedup_key == key))
        is not None
    ):
        return False
    session.add(ProjectAlias(from_dedup_key=key, to_project_id=project.id, decided_by=by))
    session.flush()
    return True


__all__ = [
    "ARBITER",
    "DECIDED_BY",
    "KeyReport",
    "natural_key",
    "remember",
    "renormalise",
    "sync",
]
