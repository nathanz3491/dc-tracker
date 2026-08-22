"""Settling suspected duplicates: a model decides, and the rails decide what it may do.

`tracker duplicates` has always proposed and never disposed, which is right —
`merge.py` puts it plainly: "a wrong merge destroys two projects and leaves no
trace, while a wrong split is visible and recoverable." The cost of that caution is
a report nobody answers. Measured on the live database: 30 suspected pairs, 29 of
them across genuinely different company names, so no alias table will ever fix
them, and every one waits on a person who has to open two rows and read their
citations.

This is that person's first pass. One model call per pair, three answers, and the
answers are not equally trusted because their consequences are not equally
reversible.

**`different` parks the pair.** Reversible by `duplicates unpark`, and the schema
was built for it: `not_duplicate.decided_by` has held `"model (0.82)"` as an
example value since migration 0016, whose comment reads "a model may park a pair; a
reader must be able to tell that one did." Parking is also the *useful* half —
`capex.rollup` holds one row of every suspected group out of the buyer table, so a
false pair removes a real campus's capacity from a published number until somebody
rules it out.

**`same` merges — but only behind flags and rails**, because a merge deletes rows.
:func:`merge_blocked` is the whole safety argument and it refuses more than it
allows:

* nothing merges without `--merge`, and nothing merges below `MERGE_CONFIDENCE`,
  which is far above the floor a park needs;
* a pair raised only by a **shared name word** never merges. `capex`'s own
  docstring on that evidence class: "a shared name word is a word";
* a pair raised only by a **cross-granularity key match** never merges. This is
  `dedup.py`'s founding invariant — "a county-level row and a city-level row are
  *never* automatically merged", because "Racine County, WI" and "Mount Pleasant,
  WI" may or may not be one project and no string comparison can tell. A model is
  not a string comparison, but it is not a person with a map either;
* two rows more than :data:`FAR_APART_KM` apart with real coordinates are not one
  site, whatever the model says. Geography outranks the model.

**Which row survives is not the model's choice.** It is the row with the most
citations, then the most fields filled, then the lower id — deterministic, so the
same input gives the same answer, and reviewable without re-reading the reasoning.
It is also nearly consequence-free: `merge_projects` recomputes every field from
the combined claims, so the surviving id decides the row number and not the values.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from tracker.models import Project
from tracker.vocab import TRACKED_FIELDS

log = logging.getLogger(__name__)

#: Below this a park is discarded and the pair is left in the report. Same floor
#: `audit resolve` uses, for the same reason: an inflated confidence must not be a
#: way to be heard.
MIN_CONFIDENCE: Final = 0.6

#: Below this a merge is refused even with `--merge`. Deliberately far above
#: `MIN_CONFIDENCE`: unparking undoes a park, and nothing undoes a merge.
MERGE_CONFIDENCE: Final = 0.9

#: Evidence classes that may carry a merge on their own. `capex.DuplicatePair.kinds`
#: names four; these are the two that are statements about the rows rather than
#: about their words or their granularity.
HARD_EVIDENCE: Final = frozenset({"tranche", "party"})

#: Two sited rows further apart than this are not one campus. Generous on purpose —
#: a campus can span a mile and a geocoder can miss by more — but a county holds
#: sites 50 km apart and this is what stops one merging into another.
FAR_APART_KM: Final = 25.0

#: Tokens per call, matching `audit.MAX_TOKENS`. Large because the budget covers
#: the model's *reasoning*, not its answer — the answer is three fields. Set to 700
#: on the first cut, which read like a broken model on the live database: five of
#: six pairs came back "unusable reply" because the reasoning never reached the
#: JSON, and the one that answered was the one with the least to think about.
MAX_TOKENS: Final = 8000

VERDICTS: Final = ("same", "different", "unclear")


def _ran_out_of_room(reply: Any) -> bool:
    """Did the budget cut the reply off, rather than the model finishing badly?

    `logic._ran_out_of_room` has the full argument for why `finish_reason` is the
    only signal that works wherever the cut landed. Shared by import rather than
    re-derived, so the two cannot disagree about what truncation looks like.
    """
    from tracker.logic import _ran_out_of_room as detect

    return detect(reply)


def km_apart(a: Project, b: Project) -> float | None:
    """Great-circle distance between two rows, or None if either is unsited.

    Written here rather than taken from a library because it is six lines and the
    alternative is a dependency for one comparison. Radius 6371 km.
    """
    if None in (a.lat, a.lon, b.lat, b.lon):
        return None
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (a.lat, a.lon, b.lat, b.lon))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class Judgement:
    """One model answer about one suspected pair."""

    verdict: str
    confidence: float
    reason: str
    #: "decided" | "declined" | "rejected"
    outcome: str = "decided"
    note: str = ""

    @property
    def decided(self) -> bool:
        return self.outcome == "decided" and self.verdict in {"same", "different"}


@dataclass
class Decision:
    """What was done about one pair, and why — including when nothing was done."""

    a_id: int
    b_id: int
    label: str
    judgement: Judgement | None = None
    #: "merged" | "parked" | "left"
    action: str = "left"
    #: Why it was left, or what the merge moved. Operator-facing.
    detail: str = ""
    kept_id: int | None = None
    removed_ids: list[int] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "a_id": self.a_id,
            "b_id": self.b_id,
            "pair": self.label,
            "action": self.action,
            "verdict": self.judgement.verdict if self.judgement else None,
            "confidence": round(self.judgement.confidence, 2) if self.judgement else None,
            "reason": self.judgement.reason if self.judgement else "",
            "detail": self.detail,
            "kept_id": self.kept_id,
            "removed_ids": list(self.removed_ids),
        }


def _citations(project: Project, cap: int = 4) -> list[str]:
    """A row's citations, longest quote first — the sentences that decide identity."""
    rows = sorted(
        project.sources or (),
        key=lambda s: (-(len(s.excerpt or "")), s.url or ""),
    )[:cap]
    out: list[str] = []
    for source in rows:
        out.append(f"      {source.source_type}: {(source.url or 'no url')[:110]}")
        if source.excerpt:
            out.append(f'        "{source.excerpt[:260]}"')
    return out or ["      (no citations)"]


def _row_block(tag: str, project: Project) -> list[str]:
    """One row as the model sees it: identity, figures, tranches, citations."""
    grain = "city" if project.city else "county" if project.county else "state only"
    lines = [
        f"  ROW {tag} — project #{project.id}",
        f"    company:   {project.company or 'unknown'}",
        f"    customer:  {project.customer or '(none named)'}",
        f"    name:      {project.name or 'unknown'}",
        f"    location:  {project.city or project.county or 'unknown'}, {project.state}"
        f"  [{grain} granularity]",
        f"    coords:    {project.lat}, {project.lon}"
        if project.lat is not None
        else "    coords:    (not geocoded)",
        f"    phase:     {project.phase}",
        f"    planned:   {project.mw_planned if project.mw_planned is not None else 'unknown'} MW"
        f"   built: {project.mw_built if project.mw_built is not None else 'unknown'} MW",
        f"    announced: {project.first_announced or 'unknown'}"
        f"   online: {project.expected_online or 'unknown'}",
    ]
    blocks = getattr(project, "blocks", ()) or ()
    if blocks:
        listed = ", ".join(
            f"{b.label}({b.block_key}{'' if b.mw is None else f', {b.mw:g} MW'})"
            for b in blocks[:6]
        )
        lines.append(f"    tranches:  {listed}")
    lines.append("    citations:")
    lines.extend(_citations(project))
    return lines


def evidence_block(a: Project, b: Project, pair: Any) -> str:
    """Both rows side by side, plus what raised the pair and how far apart they are."""
    lines = [
        f"  WHY THIS PAIR WAS RAISED: {pair.why}",
        f"  EVIDENCE CLASSES: {', '.join(pair.kinds) or 'locality only'}",
    ]
    distance = km_apart(a, b)
    if distance is None:
        lines.append("  DISTANCE: unknown — at least one row is not geocoded")
    else:
        lines.append(f"  DISTANCE: {distance:.1f} km between the stored coordinates")
    lines.append("")
    lines.extend(_row_block("A", a))
    lines.append("")
    lines.extend(_row_block("B", b))
    return "\n".join(lines)


def ask_model(
    a: Project,
    b: Project,
    pair: Any,
    *,
    extractor,
    prompt_name: str = "duplicates-resolve-v1",
) -> Judgement:
    """Ask a reasoning model whether two rows are one site. One call.

    The output is one word from a closed set, a confidence and a sentence. The
    model cannot name which row survives, cannot merge anything, and cannot edit a
    field — every consequence is decided here from its verdict.
    """
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    prompt = load_prompt(prompt_name)
    context = {
        "locality": f"{pair.locality}, {pair.state}",
        "evidence": evidence_block(a, b, pair),
    }
    try:
        reply = extractor.complete(
            system=prompt.system, user=prompt.render_user(**context), max_tokens=MAX_TOKENS
        )
    except LLMError as exc:
        log.warning("duplicates resolve failed for #%s/#%s: %s", a.id, b.id, exc)
        return Judgement("unclear", 0.0, "", outcome="rejected", note=f"call failed: {exc}")

    try:
        payload = parse_json_object(reply.text)
    except (LLMJsonError, ValueError):
        # Two failures that call for opposite responses, and reporting them as one
        # is what made a token budget look like a model that could not answer:
        # a truncated reply needs a bigger budget, malformed JSON needs a look at
        # the prompt. Borrowed from `logic._ran_out_of_room`, which learned it the
        # same way.
        if _ran_out_of_room(reply):
            log.warning(
                "duplicates resolve ran out of room while reasoning about #%s/#%s — it never "
                "reached the JSON. Raise MAX_TOKENS (currently %d); this is not a verdict.",
                a.id,
                b.id,
                MAX_TOKENS,
            )
            return Judgement(
                "unclear",
                0.0,
                "",
                outcome="rejected",
                note=f"the reply was cut off while reasoning — MAX_TOKENS is {MAX_TOKENS}",
            )
        return Judgement("unclear", 0.0, "", outcome="rejected", note="unusable reply")

    verdict = str(payload.get("verdict") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    if verdict not in VERDICTS:
        return Judgement(
            "unclear", confidence, reason, outcome="rejected", note=f"{verdict!r} is not a verdict"
        )
    if verdict == "unclear":
        return Judgement("unclear", confidence, reason, outcome="declined", note=reason)
    if not reason:
        return Judgement("unclear", confidence, reason, outcome="rejected", note="no reason given")
    if confidence < MIN_CONFIDENCE:
        return Judgement(
            "unclear",
            confidence,
            reason,
            outcome="declined",
            note=f"confidence {confidence:.2f} is below the {MIN_CONFIDENCE} floor",
        )
    return Judgement(verdict, confidence, reason)


def merge_blocked(pair: Any, judgement: Judgement, a: Project, b: Project) -> str | None:
    """Why this pair may not be merged automatically, or None if it may.

    Every branch is a rule stated somewhere else in this codebase, restated here as
    a refusal a run can print. Read this before trusting `--merge`.
    """
    if judgement.confidence < MERGE_CONFIDENCE:
        return (
            f"confidence {judgement.confidence:.2f} is below the {MERGE_CONFIDENCE} "
            "a merge needs — parked nothing, left for a person"
        )
    kinds = set(pair.kinds)
    if not (kinds & HARD_EVIDENCE):
        if "identity" in kinds:
            # dedup.py: a county-level row and a city-level row are never merged
            # automatically, because no comparison can tell whether "Racine County"
            # and "Mount Pleasant" are one project.
            return (
                "the only evidence is a cross-granularity key match; "
                "dedup refuses to merge a county row into a city row unattended"
            )
        return "the only evidence is a shared name word, which is not identity"
    distance = km_apart(a, b)
    if distance is not None and distance > FAR_APART_KM:
        return f"the stored coordinates are {distance:.0f} km apart, further than one campus spans"
    return None


def survivor(a: Project, b: Project) -> tuple[Project, Project]:
    """Which row is kept, and which is folded in. Deterministic, never the model's call.

    Citations first, because they are what a merge is trying not to lose track of —
    the row that has read more about the site keeps its id. Then filled fields,
    then the lower id, so the answer never depends on iteration order.

    Nearly consequence-free by design: `merge_projects` recomputes every field from
    the combined claims, so this decides a row number rather than a value.
    """

    def rank(p: Project) -> tuple[int, int, int]:
        filled = sum(1 for f in TRACKED_FIELDS if getattr(p, f, None) not in (None, ""))
        return (-len(p.sources or ()), -filled, p.id)

    return (a, b) if rank(a) <= rank(b) else (b, a)


def resolve_one(
    session: Session,
    pair: Any,
    *,
    extractor,
    allow_merge: bool = False,
    ask=None,
) -> Decision:
    """Decide one suspected pair and carry the decision out.

    `ask` is the person at the keyboard, called with (a, b, pair) and returning
    "same", "different", "skip", or None to hand the question to the model. A
    person's answer is trusted for a merge without the confidence floor — that is
    what `--merge` behind a keyboard means — and is recorded as `operator`.
    """
    from tracker import pairs as pairs_mod

    a = session.get(Project, pair.a_id)
    b = session.get(Project, pair.b_id)
    label = f"#{pair.a_id} {pair.a_company} — {pair.a_name} / #{pair.b_id} {pair.b_company} — {pair.b_name}"
    got = Decision(a_id=pair.a_id, b_id=pair.b_id, label=label)
    if a is None or b is None:
        got.detail = "one of the rows is gone — merged by an earlier pair in this run"
        return got

    answered_by_person = False
    if ask is not None:
        answer = (ask(a, b, pair) or "").strip().lower()
        if answer == "skip":
            got.detail = "skipped at the keyboard"
            return got
        if answer in {"same", "different"}:
            got.judgement = Judgement(answer, 1.0, "decided at the keyboard")
            answered_by_person = True

    if got.judgement is None:
        if extractor is None:
            got.detail = "nobody decided, and no model was configured"
            return got
        got.judgement = ask_model(a, b, pair, extractor=extractor)

    judged = got.judgement
    if not judged.decided:
        got.detail = judged.note or "the model would not say"
        return got

    by = "operator" if answered_by_person else f"model ({judged.confidence:.2f})"

    if judged.verdict == "different":
        pairs_mod.park(session, [a.id, b.id], reason=judged.reason, by=by)
        got.action = "parked"
        got.detail = "ruled out — capex will stop holding one of these rows back"
        return got

    # verdict == "same"
    if not allow_merge:
        got.detail = "the same site, but merging needs --merge"
        return got
    blocked = None if answered_by_person else merge_blocked(pair, judged, a, b)
    if blocked:
        got.detail = blocked
        return got

    from tracker.logic import record_decision
    from tracker.merge import merge_projects

    keep, folded = survivor(a, b)
    result = merge_projects(session, keep.id, [folded.id])
    record_decision(
        keep,
        "duplicate",
        f"folded #{folded.id} into this row",
        by=by,
        detail=judged.reason,
    )
    got.action = "merged"
    got.kept_id = keep.id
    got.removed_ids = list(result.removed)
    got.detail = (
        f"{result.sources_moved} citation(s), {result.events_moved} milestone(s) and "
        f"{result.risks_moved} obstacle(s) moved onto #{keep.id}"
    )
    return got


def resolve(
    session: Session,
    *,
    extractor,
    limit: int = 20,
    allow_merge: bool = False,
    weak: bool = False,
    ask=None,
) -> list[Decision]:
    """Work through the suspected pairs, strongest evidence first.

    `weak=False` drops the pairs raised only by a shared name word, which is the
    same default `tracker duplicates --no-weak` offers and the right one here: they
    can never be merged anyway, and asking a model about a word costs a call to be
    told what the rails already know.

    Pairs are re-read from `capex.suspected_duplicates` in one pass and then worked
    in order. A merge earlier in the run can remove a row a later pair names, which
    `resolve_one` reports rather than raising — the next run sees the new shape.
    """
    from tracker.capex import suspected_duplicates

    # Loaded with what the evidence block reads, so a 20-pair run is two queries
    # rather than sixty.
    session.scalars(
        select(Project).options(selectinload(Project.sources), selectinload(Project.blocks))
    ).all()

    found = sorted(suspected_duplicates(session), key=lambda p: (p.rank, p.a_id, p.b_id))
    if not weak:
        found = [p for p in found if set(p.kinds) - {"name"}]
    out: list[Decision] = []
    for pair in found[:limit]:
        out.append(
            resolve_one(session, pair, extractor=extractor, allow_merge=allow_merge, ask=ask)
        )
    return out


__all__ = [
    "FAR_APART_KM",
    "HARD_EVIDENCE",
    "MERGE_CONFIDENCE",
    "MIN_CONFIDENCE",
    "Decision",
    "Judgement",
    "ask_model",
    "evidence_block",
    "km_apart",
    "merge_blocked",
    "resolve",
    "resolve_one",
    "survivor",
]
