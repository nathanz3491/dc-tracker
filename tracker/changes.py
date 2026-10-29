"""What a run changed: every tracked value that differs between a snapshot taken before
it and the database now, each with the sentence now standing behind it.

**Why this exists.** The quality numbers `tracker clean` reports — the share of values
a quote backs, the rows at each tier — cannot see a wrong value that has a real quote.
On 2026-09-30 they held steady while the night's models replaced a campus's $600M
with a statewide "$20 billion+ in Ohio", kept a land price as a build investment,
stored one building's cost as a whole campus's, and wrote a campus's own operator in
as its customer. Each of those had a verbatim sentence behind it; each was wrong
about *what the sentence was about*, which only a reader catches. This puts every
change in front of one, with the sentence beside it, so reviewing a night takes a few
minutes rather than a forensic audit.

It reads; it never writes. The snapshot is the one the nightly loop
(`tracker/overnight.py`) takes with `VACUUM INTO` before round 1, or any other copy of
the database.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.confidence import DERIVED_PREFIX
from tracker.db import make_engine, session_scope
from tracker.models import Project
from tracker.vocab import BLOCK_STATUS_TO_PHASE, OPEN_RISK_STATUS, TRACKED_FIELDS

#: Compared between the two copies. `county` is not one of the twelve but is written
#: by the same paths, and a wrong county moves a row between markets.
FIELDS: tuple[str, ...] = (*TRACKED_FIELDS, "county")

#: Columns read from the snapshot, explicitly: an older snapshot may lack a column
#: added since, and selecting the whole model would fail on it.
_COLUMNS = ("id", "dedup_key", "company", "name", "city", "state", "notes", *FIELDS)

#: A claim the merge no longer counts. See `upsert.DECIDED_REASONS`.
_DECIDED = {"superseded", "misread"}

#: Why a citation, obstacle or tranche that states a value carries no sentence, in
#: the words the report prints. Keyed on `vocab.UNCONFIRMED_REASONS`; a reason not
#: listed, or none recorded, prints as a plain "no quote".
_NO_SENTENCE: dict[str, str] = {
    "no_quote": "none was offered",
    "quote_unverified": "the one offered is not in the article",
    "quote_off_target": "the one offered does not state it",
    "forecast": "the article only expects it",
    "out_of_scale": "judged too large for one campus",
    "speculative": "an extrapolation, not a committed figure",
    "contract_value": "a contract's value, not spending on the site",
}


@dataclass(frozen=True)
class Backing:
    """What stands behind one stored value: where it is, its sentence, and the path.

    Most values are a citation's own claim. Four are not, and each is traced to what
    actually set it: `blocker` to the open obstacle whose summary it is, a capacity
    or customer `blocks.reconcile` filled to the tranches it came from, a customer
    `parties.reconcile` filled to the party named in that role, and a county or a
    coordinate to the Census lookup behind it.
    """

    #: The citation's address. Empty when the row carrying the support has lost its
    #: citation, in which case `via` still names the row.
    url: str = ""
    #: The verbatim sentence. Empty when nothing quoted states the value.
    quote: str = ""
    #: What the value came through when it is not a citation's own claim: "obstacle
    #: 1481", "tranche 'Phase I'", "named as the customer". Empty for a plain claim.
    via: str = ""
    #: Without a sentence, why not, in words — or, for a derived value, the
    #: derivation itself ("Census 2020 place-to-county: X is in Y County.").
    why: str = ""
    #: Computed from reference data or from tranche statuses, never quoted.
    derived: bool = False


@dataclass(frozen=True)
class ValueChange:
    project_id: int
    label: str
    field: str
    before: Any
    after: Any
    #: The citation now behind `after`, and its sentence. Empty when nothing stands
    #: behind the new value — itself worth a reader's attention.
    url: str = ""
    quote: str = ""
    #: See `Backing`: what the value came through, and why there is no sentence.
    via: str = ""
    why: str = ""
    derived: bool = False


@dataclass
class Changes:
    values: list[ValueChange] = field(default_factory=list)
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: list[tuple[int, str]] = field(default_factory=list)
    #: Decision lines the run wrote into rows' notes: who resolved what, and why.
    decisions: list[tuple[int, str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.values or self.added or self.removed or self.decisions)


def _norm(value: Any) -> Any:
    """One spelling per value, so a date and its ISO string, or 2.0 and 2, compare equal."""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def _label(row: Any) -> str:
    where = row.city or getattr(row, "county", None) or ""
    return f"{row.company} — {row.name}" + (f" ({where}, {row.state})" if where else "")


def _load(blob: str | None) -> dict:
    try:
        value = json.loads(blob or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def backing(project: Any, field_name: str) -> Backing:
    """What stands behind the row's current value for `field_name`.

    A sentence is preferred over none, then a derivation over an unquoted claim.
    An empty `Backing` when nothing states the value — a value nothing currently
    supports, and the one thing the report shouts about.

    **Each value is traced to what set it, not only to citations.** The 2026-10-13
    report printed "NOTHING" against a county the merge spelled out and two customers
    copied up from tranches; fixing those two left the rest. On 2026-10-19 it printed
    "NOTHING" against the blockers of #5, #183 and #1328 and #1608's built capacity,
    and "no quote" against the other seventeen blockers that changed — when `blocker`
    is derived from the obstacle rows (`upsert._derive_blocker`), which carry the
    sentence, and no citation has ever held a quote for it: of 817 citations claiming
    a blocker on that night's copy, none did. Traced to the obstacles, sixteen of the
    twenty have a sentence; #183's had been confirmed that same night. #1608's 3 MW
    was `blocks.reconcile` filling the row from its one quoted tranche. A line that
    cries wolf is one a reader learns to skip.
    """
    value = _norm(getattr(project, field_name))
    urls = {s.id: s.url for s in getattr(project, "sources", None) or ()}
    found = [
        *(_obstacles(project, value, urls) if field_name == "blocker" else ()),
        *_claims(project, field_name, value),
        *_reconciled(project, field_name, value, urls),
    ]
    return (
        next((b for b in found if b.quote), None)
        or next((b for b in found if b.derived), None)
        or (found[0] if found else Backing())
    )


def _claims(project: Any, field_name: str, value: Any) -> Iterator[Backing]:
    """One per citation whose standing claim is `value`."""
    same = _same_county if field_name == "county" else (lambda a, b: a == b)
    for source in getattr(project, "sources", None) or ():
        claims = _load(source.claims)
        if field_name not in claims or not same(_norm(claims[field_name]), value):
            continue
        reason = _load(source.unconfirmed_reasons).get(field_name)
        if reason in _DECIDED:
            continue
        if (getattr(source, "extractor", None) or "").startswith(DERIVED_PREFIX):
            # No sentence was ever possible: the Census publishes a table, not prose.
            # The excerpt states what was looked up and from where, which is the
            # checkable thing — "no quote" would read as a source that failed a gate.
            yield Backing(
                source.url,
                via="derived from reference data",
                why=getattr(source, "excerpt", None) or "",
                derived=True,
            )
            continue
        quote = str(_load(source.quotes).get(field_name) or "").strip()
        yield Backing(source.url, quote, why="" if quote else _NO_SENTENCE.get(reason or "", ""))


def _obstacles(project: Any, value: Any, urls: dict[int, str]) -> Iterator[Backing]:
    """The open obstacles whose summary is the stored `blocker`, the oldest first.

    `upsert._derive_blocker` writes the summary of one open obstacle into `blocker`,
    so the sentence behind it is that obstacle's own `quote` — never a citation's,
    since the citation's `blocker` claim is a single line of what the article said and
    one article can raise several obstacles.
    """
    risks = [
        r
        for r in getattr(project, "risks", None) or ()
        if r.status == OPEN_RISK_STATUS and _norm(r.summary) == value
    ]
    for risk in sorted(risks, key=lambda r: r.id or 0):
        quote = (risk.quote or "").strip()
        yield Backing(
            urls.get(risk.source_id, ""),
            quote,
            via=f"obstacle {risk.id}",
            why="" if quote else _NO_SENTENCE.get(risk.unconfirmed or "", ""),
        )


def _reconciled(
    project: Any, field_name: str, value: Any, urls: dict[int, str]
) -> Iterator[Backing]:
    """What a reconcile pass filled the field from, when no claim did.

    `parties.reconcile` and `blocks.reconcile` fill a null and never overwrite —
    `customer` from a party or a tranche, `mw_planned` and `mw_built` from the
    tranches' sum — and `blocks.phase_after_blocks` raises `phase` from the named
    tranches' status. Each carries its own sentence except the status, which no
    tranche quotes.
    """
    blocks = list(getattr(project, "blocks", None) or ())
    if field_name == "customer" and isinstance(value, str):
        wanted = value.casefold()
        for party in getattr(project, "parties", None) or ():
            if party.role != "customer" or party.unconfirmed is not None:
                continue
            if (party.name or "").strip().casefold() == wanted:
                quote = (party.quote or "").strip()
                yield Backing(urls.get(party.source_id, ""), quote, via="named as the customer")
        for block in blocks:
            if (block.customer or "").strip().casefold() == wanted:
                yield _tranche(block, "customer", urls)
    elif field_name in ("mw_planned", "mw_built") and isinstance(value, int | float) and blocks:
        from tracker.blocks import rollup, structure

        # The tree `reconcile` filled it from, so the sum checked here is that sum.
        got = rollup(blocks, structure(project))
        total = got.mw_planned if field_name == "mw_planned" else got.mw_built
        if total is None or not math.isclose(total, value, rel_tol=1e-9, abs_tol=1e-6):
            return
        keys = got.planned_from if field_name == "mw_planned" else got.built_from
        summed = [b for b in blocks if b.block_key in keys]
        if len(summed) == 1:
            yield _tranche(summed[0], "mw", urls)
            return
        # Several tranches add up to the figure. One sentence is shown — the first
        # one any of them carries — and every tranche is named with its megawatts,
        # so the arithmetic can be checked against the line above it.
        named = ", ".join(f"{b.label!r} {b.mw:g} MW" for b in summed[:4])
        more = f" and {len(summed) - 4} more" if len(summed) > 4 else ""
        quoted = next((b for b in summed if _load(b.quotes).get("mw")), summed[0])
        first = _tranche(quoted, "mw", urls)
        yield Backing(
            first.url,
            first.quote,
            via=f"the sum of {len(summed)} tranches: {named}{more}"
            + (f"; the sentence is {quoted.label!r}'s" if first.quote else ""),
            why="" if first.quote else "none of them carries one",
        )
    elif field_name == "phase" and isinstance(value, str):
        lifting = [
            b
            for b in blocks
            if not getattr(b, "generic", False) and BLOCK_STATUS_TO_PHASE.get(b.status) == value
        ]
        if lifting:
            named = ", ".join(repr(b.label) for b in lifting[:4])
            more = f" and {len(lifting) - 4} more" if len(lifting) > 4 else ""
            yield Backing(
                via="its tranches' status",
                why=f"{named}{more} ({lifting[0].status})",
                derived=True,
            )


def _tranche(block: Any, key: str, urls: dict[int, str]) -> Backing:
    quote = str(_load(block.quotes).get(key) or "").strip()
    return Backing(
        urls.get(block.source_id, ""),
        quote,
        via=f"tranche {block.label!r}",
        why="" if quote else "the tranche carries none",
    )


def _same_county(claimed: Any, stored: Any) -> bool:
    from tracker.dedup import county_key

    return isinstance(claimed, str) and county_key(claimed) == county_key(stored)


def _snapshot_rows(path: Path) -> dict[int, Any]:
    engine = make_engine(path, readonly=True)
    try:
        with session_scope(engine, commit=False) as s:
            columns = [getattr(Project, name) for name in _COLUMNS]
            return {row.id: row for row in s.execute(select(*columns)).all()}
    finally:
        engine.dispose()


def diff(snapshot: Path, session: Session) -> Changes:
    """Everything that differs between `snapshot` and the database `session` reads."""
    before = _snapshot_rows(snapshot)
    out = Changes()
    now = {p.id: p for p in session.scalars(select(Project))}
    for pid, project in sorted(now.items()):
        old = before.get(pid)
        # A merge deletes a row, and before migration 0032 SQLite handed its id to the
        # next insert; backups from then hold such ids. So an id alone does not say
        # "same row": the identity key has to match too.
        if old is None or old.dedup_key != project.dedup_key:
            out.added.append((pid, _label(project)))
            if old is not None:
                out.removed.append((pid, _label(old)))
            continue
        for name in FIELDS:
            was, is_ = _norm(getattr(old, name)), _norm(getattr(project, name))
            if was != is_:
                b = backing(project, name) if is_ is not None else Backing()
                out.values.append(
                    ValueChange(
                        pid,
                        _label(project),
                        name,
                        was,
                        is_,
                        b.url,
                        b.quote,
                        b.via,
                        b.why,
                        b.derived,
                    )
                )
        old_lines = set((old.notes or "").splitlines())
        for line in (project.notes or "").splitlines():
            if line.strip() and line not in old_lines and " resolved `" in line:
                out.decisions.append((pid, _label(project), line.strip()))
    for pid in sorted(before.keys() - now.keys()):
        out.removed.append((pid, _label(before[pid])))
    return out


def _fmt(value: Any) -> str:
    if value is None:
        return "empty"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def _behind(c: ValueChange) -> str:
    """The "behind it" line's text: the sentence, or what stands in for one, and where.

    "no quote" only when something states the value and carries no sentence, with
    the reason when one was recorded. "NOTHING" only when nothing states it at all.
    A quote is printed on one line: a sentence lifted from a page can carry the
    page's line breaks, and one did on 2026-10-19 ("Total Capacity:", a blank line,
    "600 MW"), splitting the report's own lines.
    """
    where = c.url[:90]
    if c.quote:
        text = '"' + " ".join(c.quote.split())[:220] + '"'
    elif c.derived:
        text = f"{c.via}, not a sentence — " + " ".join(c.why.split())[:220]
    elif where or c.via:
        text = "no quote" + (f" ({c.why})" if c.why else "")
    else:
        return "NOTHING — no citation, obstacle or tranche states this value"
    named = f" ({c.via})" if c.via and not c.derived else ""
    return text + (f" — {where}" if where else "") + named


def render(changes: Changes, *, since: str = "") -> list[str]:
    """The report's lines, plain text, for a log a person reads in the morning."""
    head = f"what changed{f' since {since}' if since else ''}"
    if not changes:
        return [f"{head}: nothing"]
    unchanged = sum(1 for _, _, line in changes.decisions if _left_as_it_was(line))
    lines = [
        f"{head}: {len(changes.values)} value(s) on "
        f"{len({c.project_id for c in changes.values})} row(s), "
        f"{len(changes.added)} row(s) created, {len(changes.removed)} removed, "
        f"{len(changes.decisions)} decision(s) noted"
        + (f", {unchanged} of them leaving the value as it was" if unchanged else "")
    ]
    for c in changes.values:
        lines.append(
            f"  #{c.project_id} {c.label[:60]}  {c.field}: {_fmt(c.before)} -> {_fmt(c.after)}"
        )
        if c.after is None:
            continue
        lines.append(f"      behind it: {_behind(c)}")
    for pid, label in changes.added:
        lines.append(f"  created  #{pid} {label[:80]}")
    for pid, label in changes.removed:
        lines.append(f"  removed  #{pid} {label[:80]}")
    for pid, label, line in changes.decisions:
        mark = "(value unchanged) " if _left_as_it_was(line) else ""
        lines.append(f"  decided  #{pid} {label[:40]}: {mark}{line[:240]}")
    return lines


def _left_as_it_was(decision: str) -> bool:
    """A noted ruling whose edit put back the value it started from — `750 -> 750`.

    It struck a citation while another states the same value, so the row reads as it
    did. Marked rather than dropped: a claim did leave the merge, and the finding it
    answered is still open.
    """
    from tracker.audit import left_as_it_was

    _, _, edit = decision.partition("`: ")
    return left_as_it_was(edit)


__all__ = ["FIELDS", "Backing", "Changes", "ValueChange", "backing", "diff", "render"]
