"""Ruling how a campus's tranches nest, where arithmetic cannot: a model's or a person's.

`blocks.structure` settles what it can without judgement — one tranche under several
names, a stated parent that is itself a tranche, a campus-wide tranche whose parts
fit inside it. On the 10-21 copy that settled 22 of the 128 campuses whose tranches
described them more than once. The rest need someone to read the sentences:
Abilene's 45 tranches describe the campus at five grains and four other sites
besides; Sweetwater's include IREN's Childress campus; Hyperion's include a 2,000 MW
"Richland Parish" that is Phase 1 again.

**The review** (`tracker tranches review`, nightly after `logic`) puts one campus at a
time in front of the judgement model: every tranche with its figures, its parent, the
quoted sentences behind it, and how it is linked now. The model answers per tranche —
part of, same as, separate, superseded, elsewhere — and an answer is applied only
when it names tranches that exist, would not make one part of itself, carries a
confidence of at least `MIN_CONFIDENCE`, and does not touch a tranche a person ruled
on. A model's rulings on a campus are replaced wholesale by its next review of it, so
they never pile up; a person's are never touched.

**What a review costs** is one call per campus, worst first by the megawatts at stake.
A campus that still does not add up afterwards is remembered with a hash of the
tranches it was judged on (`declines`, kind `nest`) and not asked about again until a
re-read changes them, or for `declines.COOLDOWN_DAYS`.

**A person** rules with `tracker tranches rule` or on the project page (admins), and
takes a ruling back with `tracker tranches undo`. A person's ruling outranks a
model's and every automatic rule, and is the one thing the review will not revisit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker import blocks as blocks_mod
from tracker.models import BlockNesting, Project, utcnow
from tracker.vocab import NEST_RELATIONS, NEST_TARGETED

log = logging.getLogger(__name__)

#: Room for a reasoning model to read every tranche of a 45-tranche campus.
MAX_TOKENS: Final = 8192

#: Below this a ruling is discarded and the tranche left as it was. The bar
#: `riskcheck` uses: a ruling here moves a company's figures, so a guess costs more
#: than a skip.
MIN_CONFIDENCE: Final = 0.65

#: The declines kind a campus left unsettled is remembered under.
KIND: Final = "nest"

#: Who a model's ruling is recorded as. A person's carries their account name.
MODEL: Final = "model"

#: Characters of each quoted sentence shown. A tranche usually carries one or two.
QUOTE_CHARS: Final = 240

#: Megawatts below which a campus is settled: the accounting rounds to half of one.
_SETTLED_MW: Final = 0.51


class RulingError(ValueError):
    """A ruling that cannot be recorded, in words the person who asked can act on."""


@dataclass(frozen=True)
class Stake:
    """What still does not add up on one campus, in megawatts."""

    overlap: float = 0.0
    unplaced: float = 0.0
    live_over: float = 0.0

    @property
    def total(self) -> float:
        return self.overlap + self.unplaced + self.live_over

    @property
    def settled(self) -> bool:
        return self.total < _SETTLED_MW

    def as_json(self) -> dict[str, float]:
        return {
            "overlap_mw": round(self.overlap, 1),
            "unplaced_mw": round(self.unplaced, 1),
            "live_over_mw": round(self.live_over, 1),
        }


def stake(project: Any) -> Stake:
    """Counted twice, tied to no facility, and live past what runs — the three
    relations (`invariants`) a hierarchy is what makes hold."""
    got = blocks_mod.account(project)
    lines = {r.reason: r.mw for r in got.residuals}
    delivering = blocks_mod.delivering(project, got)
    live_over = max(0.0, got.live_mw - delivering) if delivering is not None else 0.0
    return Stake(lines.get("overlap", 0.0), lines.get("unplaceable", 0.0), live_over)


def candidates(session: Session, *, project_id: int | None = None) -> list[tuple[Project, Stake]]:
    """Campuses whose tranches still do not add up, worst first."""
    from tracker.export import fetch_projects

    projects = fetch_projects(session, only=[project_id] if project_id is not None else None)
    out = []
    for project in projects:
        if not project.blocks:
            continue
        at_stake = stake(project)
        if not at_stake.settled:
            out.append((project, at_stake))
    out.sort(key=lambda pair: (-pair[1].total, pair[0].id))
    return out


def tranches(project: Any) -> list[Any]:
    """The tranches a review is shown, in a fixed order: largest first, then by name.

    Generation is left out — it is the utility's, already outside every sum, and a
    model asked how a gas plant nests in a campus can only get it wrong.
    """
    return sorted(
        (
            b
            for b in (getattr(project, "blocks", ()) or ())
            if not blocks_mod.is_generation(b.label, getattr(b, "parent", None))
        ),
        key=lambda b: (-(b.mw or 0.0), (b.label or "").lower(), b.block_key),
    )


def person_ruled(project: Any) -> dict[str, BlockNesting]:
    """Tranche key -> a person's ruling on it. The review never touches these."""
    return {
        row.block_key: row
        for row in (getattr(project, "nesting", ()) or ())
        if row.decided_by != MODEL
    }


def evidence(project: Any) -> str:
    """A hash of what a review of this campus is shown: its tranches and fixed rulings."""
    from tracker import declines

    rows = [
        (
            b.block_key,
            b.label,
            getattr(b, "parent", None),
            b.mw,
            b.status,
            b.customer,
            getattr(b, "unconfirmed_fields", None),
            getattr(b, "quotes", None),
        )
        for b in tranches(project)
    ]
    fixed = sorted((k, r.relation, r.target_key or "") for k, r in person_ruled(project).items())
    return declines.fingerprint(project.mw_planned, project.mw_built, rows, fixed)


def fresh(
    session: Session, pairs: list[tuple[Project, Stake]], *, again: bool = False
) -> tuple[list[tuple[Project, Stake]], int]:
    """The campuses not already reviewed on the same tranches, and how many were."""
    from tracker import declines

    known = declines.load(session, KIND)
    return declines.split(
        pairs, known, key=lambda pair: (str(pair[0].id), evidence(pair[0])), again=again
    )


# --- what the model is shown --------------------------------------------------


def _quotes(block: Any) -> list[str]:
    import json

    try:
        quotes = json.loads(getattr(block, "quotes", None) or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(quotes, dict):
        return []
    # Capacity and state first: those are the sentences that say what a tranche is.
    order = ["mw", "status", "label", "customer", "expected_online", "energized_on"]
    keys = [k for k in order if k in quotes] + sorted(k for k in quotes if k not in order)
    seen: list[str] = []
    for key in keys:
        text = " ".join(str(quotes[key] or "").split())
        if text and text not in seen:
            seen.append(text)
    return seen[:3]


def ids(project: Any) -> dict[str, Any]:
    """`T1`, `T2`, ... -> tranche, in the order `tranches` shows them."""
    return {f"T{i}": b for i, b in enumerate(tranches(project), start=1)}


def build_context(project: Any) -> dict[str, str]:
    """Everything the model may reason from, as prompt variables."""
    nested = blocks_mod.structure(project)
    shown = ids(project)
    by_key = {b.block_key: tid for tid, b in shown.items()}
    sources = {s.id: s for s in (getattr(project, "sources", ()) or ())}
    fixed = person_ruled(project)

    lines: list[str] = []
    for tid, block in shown.items():
        mw = "no figure"
        if block.mw is not None:
            mw = f"{block.mw:g} MW" + ("" if blocks_mod.mw_is_confirmed(block) else " (unquoted)")
        head = f"{tid}  {block.label}"
        if getattr(block, "parent", None):
            head += f"   [parent named: {block.parent}]"
        lines.append(head)
        lines.append(
            f"    {mw}, {block.status}" + (f", customer {block.customer}" if block.customer else "")
        )
        when = getattr(block, "energized_on", None) or getattr(block, "expected_online", None)
        if when:
            lines.append(f"    date: {when}")
        link = nested.link(block.block_key)
        if block.block_key in fixed:
            row = fixed[block.block_key]
            target = by_key.get(row.target_key or "", row.target_key or "")
            lines.append(
                f"    PERSON ruled: {row.relation}{' ' + target if target else ''} — fixed"
            )
        elif link is not None:
            target = by_key.get(link.target or "", link.target or "")
            who = "automatic" if link.automatic else link.by
            lines.append(f"    now: {link.relation}{' ' + target if target else ''} ({who})")
        for quote in _quotes(block):
            lines.append(f'    quoted: "{quote[:QUOTE_CHARS]}"')
        source = sources.get(getattr(block, "source_id", None))
        if source is not None:
            published = getattr(source, "published_at", None) or getattr(source, "fetched_at", None)
            day = f" ({str(published)[:10]})" if published else ""
            lines.append(f"    source: {source.url[:110]}{day}")

    got = blocks_mod.account(project, nested)
    arithmetic = ", ".join(f"{r.reason} {r.mw:,.0f} MW" for r in got.residuals) or "it adds up"

    def show(value: Any) -> str:
        return "unknown" if value is None else f"{value:g}"

    return {
        "project_id": str(project.id),
        "company": project.company or "unknown",
        "name": project.name or "unknown",
        "location": f"{project.city or project.county or 'unknown'}, {project.state}",
        "phase": project.phase or "unknown",
        "mw_planned": show(project.mw_planned),
        "mw_built": show(project.mw_built),
        "arithmetic": f"{got.counted_mw:,.0f} MW counted; {arithmetic}",
        "tranches": "\n".join(lines),
    }


# --- the answer --------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """One ruling the model offered, already checked."""

    key: str
    relation: str
    target: str | None
    confidence: float
    reason: str


@dataclass
class Review:
    """One model reading of one campus."""

    project_id: int
    proposals: list[Proposal] = field(default_factory=list)
    #: `(what was offered, why it was not used)`, for the run's own report.
    refused: list[tuple[str, str]] = field(default_factory=list)
    #: Set when the call failed or the reply was unusable; nothing is applied then.
    failure: str = ""


def parse(project: Any, payload: dict[str, Any]) -> Review:
    """Check every ruling in a reply against the campus. Never trusts an id."""
    review = Review(project.id)
    shown = ids(project)
    fixed = person_ruled(project)
    seen: set[str] = set()
    items = payload.get("tranches")
    if not isinstance(items, list):
        review.failure = "no tranches in the reply"
        return review
    for item in items:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip().upper()
        relation = str(item.get("relation") or "").strip().lower()
        target_id = str(item.get("of") or "").strip().upper() or None
        reason = " ".join(str(item.get("reason") or "").split())[:300]
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        said = f"{tid} {relation}{' ' + target_id if target_id else ''}"
        block = shown.get(tid)
        if block is None:
            review.refused.append((said, "no such tranche"))
            continue
        if tid in seen:
            review.refused.append((said, "ruled twice"))
            continue
        if relation not in NEST_RELATIONS:
            review.refused.append((said, f"{relation!r} is not a relation"))
            continue
        if block.block_key in fixed:
            review.refused.append((said, "a person ruled on it"))
            continue
        target = shown.get(target_id) if target_id else None
        if relation in NEST_TARGETED and target is None:
            review.refused.append((said, "names no tranche it is part of or the same as"))
            continue
        if relation not in NEST_TARGETED and relation != "superseded":
            target = None
        if target is block:
            review.refused.append((said, "a tranche cannot relate to itself"))
            continue
        if confidence < MIN_CONFIDENCE:
            review.refused.append((said, f"confidence {confidence:.2f} under {MIN_CONFIDENCE}"))
            continue
        seen.add(tid)
        review.proposals.append(
            Proposal(
                block.block_key,
                relation,
                target.block_key if target is not None else None,
                min(confidence, 1.0),
                reason,
            )
        )
    return review


def review(project: Any, *, extractor, prompt_name: str = "nest-v1") -> Review:
    """Ask the model how this campus's tranches relate. One call."""
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    prompt = load_prompt(prompt_name)
    try:
        reply = extractor.complete(
            system=prompt.system,
            user=prompt.render_user(**build_context(project)),
            max_tokens=MAX_TOKENS,
        )
    except LLMError as exc:
        log.warning("tranche review failed for project %s: %s", project.id, exc)
        return Review(project.id, failure=f"call failed: {str(exc)[:160]}")
    try:
        payload = parse_json_object(reply.text)
    except (LLMJsonError, ValueError):
        return Review(project.id, failure="unusable reply")
    return parse(project, payload)


# --- writing it down -----------------------------------------------------------


def _links_without(project: Any, key: str | None = None) -> dict[str, blocks_mod.Link]:
    """The stored rulings in force, a person's first, leaving out one tranche's."""
    out: dict[str, blocks_mod.Link] = {}
    for row in sorted(
        getattr(project, "nesting", ()) or (),
        key=lambda r: (r.decided_by == MODEL, r.block_key),
    ):
        if row.block_key == key:
            continue
        out[row.block_key] = blocks_mod.Link(row.block_key, row.relation, row.target_key)
    return out


def apply(session: Session, project: Project, found: Review, *, by: str = MODEL) -> int:
    """Replace the model's rulings on this campus with `found`. Returns rulings written.

    A person's rulings stay. A proposal that would close a loop with the rulings
    already written is dropped, in the order the model gave them.
    """
    for row in list(project.nesting):
        if row.decided_by == MODEL:
            project.nesting.remove(row)
    session.flush()
    links = _links_without(project)
    written = 0
    now = utcnow()
    for proposal in found.proposals:
        if proposal.key in links:
            continue
        if blocks_mod.closes_cycle(links, proposal.key, proposal.target):
            found.refused.append((proposal.key, "would make a tranche part of itself"))
            continue
        project.nesting.append(
            BlockNesting(
                block_key=proposal.key,
                relation=proposal.relation,
                target_key=proposal.target,
                reason=proposal.reason or None,
                confidence=round(proposal.confidence, 2),
                decided_by=by,
                decided_at=now,
            )
        )
        links[proposal.key] = blocks_mod.Link(proposal.key, proposal.relation, proposal.target)
        written += 1
    session.flush()
    return written


@dataclass
class Outcome:
    """What one campus's review did."""

    project_id: int
    project: str
    before: Stake
    after: Stake
    written: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)
    #: "settled" | "improved" | "unchanged" | "error"
    result: str = "unchanged"
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project": self.project,
            "result": self.result,
            "written": self.written,
            "before": self.before.as_json(),
            "after": self.after.as_json(),
            "refused": [{"offered": a, "why": b} for a, b in self.refused],
            "note": self.note,
        }


def run(
    session: Session,
    pairs: list[tuple[Project, Stake]],
    *,
    extractor,
    write: bool = True,
    on_each=None,
) -> list[Outcome]:
    """Review each campus and record what it rules. One model call per campus.

    `write=False` applies each answer inside a savepoint, measures it, and rolls it
    back — the same code decides either way, so a preview cannot report an outcome
    the real run would not produce.
    """
    from tracker import declines

    out: list[Outcome] = []
    for project, before in pairs:
        shown = evidence(project)
        found = review(project, extractor=extractor)
        outcome = Outcome(project.id, f"{project.company} — {project.name}", before, before)
        if found.failure:
            outcome.result, outcome.note = "error", found.failure
            if write:
                declines.record(
                    session,
                    KIND,
                    str(project.id),
                    shown,
                    outcome=declines.ERROR,
                    reason=found.failure,
                    by=MODEL,
                )
        else:
            savepoint = None if write else session.begin_nested()
            outcome.written = apply(session, project, found)
            session.expire(project, ["nesting"])
            outcome.after = stake(project)
            outcome.refused = list(found.refused)
            if outcome.after.settled:
                outcome.result = "settled"
            elif outcome.after.total < before.total - _SETTLED_MW:
                outcome.result = "improved"
            if savepoint is not None:
                savepoint.rollback()
                session.expire(project, ["nesting"])
            elif outcome.after.settled:
                declines.forget(session, KIND, str(project.id))
            else:
                # Judged on these tranches and still open: not asked again until a
                # re-read changes them, or the cooldown lapses.
                declines.record(
                    session,
                    KIND,
                    str(project.id),
                    shown,
                    outcome=outcome.result,
                    reason=f"{outcome.after.total:,.0f} MW still open after {outcome.written} ruling(s)",
                    by=MODEL,
                )
        if on_each is not None:
            on_each(outcome)
        out.append(outcome)
    return out


# --- a person's hand -----------------------------------------------------------


def find_key(project: Any, text: str) -> str | None:
    """A tranche key from what a person typed: the key, or the label exactly."""
    wanted = (text or "").strip()
    present = list(getattr(project, "blocks", ()) or ())
    for block in present:
        if block.block_key == wanted:
            return block.block_key
    folded = wanted.casefold()
    matches = [b.block_key for b in present if (b.label or "").strip().casefold() == folded]
    return matches[0] if len(matches) == 1 else None


def rule(
    session: Session,
    project: Project,
    key: str,
    relation: str,
    target: str | None = None,
    *,
    by: str,
    reason: str | None = None,
) -> BlockNesting:
    """Record a person's ruling on one tranche, replacing any earlier one on it."""
    if by == MODEL or not by.strip():
        raise RulingError("a person's ruling needs the person's name")
    if relation not in NEST_RELATIONS:
        raise RulingError(f"relation must be one of {', '.join(NEST_RELATIONS)}")
    present = {b.block_key for b in project.blocks}
    if key not in present:
        raise RulingError(f"#{project.id} has no tranche {key!r}")
    if relation in NEST_TARGETED and not target:
        raise RulingError(f"{relation} needs the tranche it relates to")
    if relation not in NEST_TARGETED and relation != "superseded":
        target = None
    if target is not None:
        if target not in present:
            raise RulingError(f"#{project.id} has no tranche {target!r}")
        if target == key:
            raise RulingError("a tranche cannot relate to itself")
        if blocks_mod.closes_cycle(_links_without(project, key), key, target):
            raise RulingError(f"{key} would end up part of itself")
    row = next((r for r in project.nesting if r.block_key == key), None)
    if row is None:
        row = BlockNesting(block_key=key, relation=relation)
        project.nesting.append(row)
    row.relation = relation
    row.target_key = target
    row.reason = " ".join((reason or "").split())[:300] or None
    row.confidence = None
    row.decided_by = by.strip()
    row.decided_at = utcnow()
    session.flush()
    return row


def undo(session: Session, project: Project, key: str) -> bool:
    """Take back the ruling on one tranche, whoever made it. True if there was one."""
    row = next((r for r in project.nesting if r.block_key == key), None)
    if row is None:
        return False
    project.nesting.remove(row)
    session.flush()
    return True


def project_with_rulings(session: Session, project_id: int) -> Project | None:
    from sqlalchemy.orm import selectinload

    return session.scalar(
        select(Project)
        .options(
            selectinload(Project.blocks),
            selectinload(Project.nesting),
            selectinload(Project.sources),
        )
        .where(Project.id == project_id)
    )


__all__ = [
    "KIND",
    "MAX_TOKENS",
    "MIN_CONFIDENCE",
    "MODEL",
    "Outcome",
    "Proposal",
    "Review",
    "RulingError",
    "Stake",
    "apply",
    "build_context",
    "candidates",
    "evidence",
    "find_key",
    "fresh",
    "ids",
    "parse",
    "person_ruled",
    "project_with_rulings",
    "review",
    "rule",
    "run",
    "stake",
    "tranches",
    "undo",
]
