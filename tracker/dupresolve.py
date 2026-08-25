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
  not a string comparison, but it is not a person with a map either. What granularity
  no longer does is *hide* the rest of the evidence: `capex`'s second pass records
  every signal now, so `identity+tranche` is a different question from `identity`;
* a pair whose only shared tranche is a **market sequence** never merges —
  `dedup.is_market_sequence`. `iad-3`, `hillsboro-1`, `chicago-2`: a market and a
  number that restarts at one for every operator. The key is still reported,
  because it is the only thing connecting some real duplicates, and it is not
  enough to delete a row on;
* two names differing only by an **ordinal** never merge —
  `dedup.sibling_ordinals`. Neighbouring phases of one development share an
  operator, a market and often a tranche key, and differ in one digit;
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

from tracker.dedup import is_market_sequence, sibling_ordinals
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
#: names five; these are the three that are statements about the *building* rather
#: than about a word or about a place.
#:
#: **`exact` joined the set when it started existing.** Two rows carrying the same
#: company and the same name are a stronger claim than a shared tranche key, and
#: measured on the live database six pairs were in that position while being
#: reported under the weakest class the report has — `distinctive_name_tokens`
#: strips generic words and the locality, so two identical names produced no name
#: evidence at all. See `dedup.exact_identity`.
#:
#: **`identity` is still not in here**, and that is `dedup.py`'s founding
#: invariant rather than caution: a county-level row and a city-level row may be
#: one site or two and no comparison of strings can tell. What changed is that a
#: pair can now carry granularity *and* something else — `capex`'s second pass used
#: to record granularity alone however much else was true — so `identity+tranche`
#: is decidable while `identity` is not.
HARD_EVIDENCE: Final = frozenset({"exact", "tranche", "party"})

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


#: Citations shown per row, and how many may come from one publisher. The median
#: row holds 7 sources and the fattest holds 78, so this is a window and the rule
#: that picks what goes in it matters more than its size.
CITATION_CAP: Final = 8
PER_DOMAIN_CAP: Final = 2

#: Characters of each quote. 260 truncated mid-sentence often enough to matter.
EXCERPT_CHARS: Final = 400


def _domain(url: str | None) -> str:
    """The publisher, for the diversity cap. Cheap split rather than urllib."""
    return (url or "").split("//")[-1].split("/")[0].lower().removeprefix("www.")


def _citations(project: Project, cap: int = CITATION_CAP) -> list[str]:
    """A row's citations — newest first, no more than two per publisher.

    **Longest-quote-first was the wrong rule and it was costing the answer.** The
    sentence that settles whether two rows are one site is usually short — a street
    address, a "formerly known as", a county named in passing — while the longest
    quote on a row is typically a paragraph of context from whichever article the
    extractor happened to read most of. Sorting by length put four quotes from one
    press release in the window and left the identifying sentence out of it.

    Newest first because a campus's identity is restated every time it is written
    about, and the most recent article is the one that knows about the rename. At
    most two per domain because `confidence.py` counts independence by domain, and
    the same reasoning applies to a window: eight quotes from one publisher are one
    publisher's account of the site.
    """
    ranked = sorted(
        project.sources or (),
        key=lambda s: (
            -(s.published_at.timestamp() if s.published_at else 0),
            -(s.fetched_at.timestamp() if s.fetched_at else 0),
            s.url or "",
        ),
    )
    seen: dict[str, int] = {}
    out: list[str] = []
    for source in ranked:
        domain = _domain(source.url)
        if seen.get(domain, 0) >= PER_DOMAIN_CAP:
            continue
        seen[domain] = seen.get(domain, 0) + 1
        out.append(f"      {source.source_type}: {(source.url or 'no url')[:110]}")
        if source.excerpt:
            out.append(f'        "{source.excerpt[:EXCERPT_CHARS]}"')
        if len([line for line in out if not line.startswith('        "')]) >= cap:
            break
    return out or ["      (no citations)"]


def _row_block(tag: str, project: Project) -> list[str]:
    """One row as the model sees it: identity, figures, tranches, citations."""
    grain = "city" if project.city else "county" if project.county else "state only"
    # Both fields, always. Printing `city or county` meant a city row never showed
    # its county, so the one question being asked — is this town inside that county
    # — had to be recovered from the raw dedup key in the WHY line above.
    where = ", ".join(part for part in (project.city, project.county, project.state) if part)
    lines = [
        f"  ROW {tag} — project #{project.id}",
        f"    company:   {project.company or 'unknown'}",
        f"    customer:  {project.customer or '(none named)'}",
        f"    name:      {project.name or 'unknown'}",
        f"    location:  {where or 'unknown'}  [{grain} granularity]",
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
        # Saying the number without saying what it measures invited the answer
        # backwards. `ingest/geo.py` derives coordinates from the Census *place
        # centroid* — "a place centroid is not the site", in its own words — so two
        # rows in one town read 0.0 km apart whether they are one building or two
        # miles apart, and 245 pairs on the live database sit within 3 km of each
        # other for that reason alone. The figure can refute and cannot confirm.
        lines.append(
            f"  DISTANCE: {distance:.1f} km between the stored coordinates — these are "
            "place centroids, not site locations, so a small number means one town "
            "and says nothing about one building. A large one is still decisive."
        )
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
    prompt_name: str = "duplicates-resolve-v2",
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
    if kinds & HARD_EVIDENCE == {"tranche"} and all(
        is_market_sequence(key, localities={a.city, a.county, b.city, b.county})
        for key in pair.shared_blocks
    ):
        # `iad-3`, `hillsboro-1`, `chicago-2`: a market and a sequence number, and
        # the sequence restarts at one for every operator. The key is worth showing
        # a reader — it is how IREN's Sweetwater campus, stored twice across a
        # rename, is connected at all — and it is not worth deleting a row over:
        # `hillsboro-1` is held by Flexential's Hillsboro site and NTT's.
        return (
            f"the only shared tranche is {', '.join(pair.shared_blocks)}, which names a "
            "market and a sequence number rather than a building"
        )
    if sibling_ordinals(a.name, b.name):
        # The failure this exists for, found while measuring the change that made
        # a shared tranche carry a merge across localities: Applied Digital's
        # `Polaris Forge 1` in Ellendale and `Polaris Forge 2` in Harwood both hold
        # `forge-2.polaris`, because one article listed the pair. Two real campuses,
        # every signal in agreement, and one digit between them.
        return (
            f"{a.name!r} and {b.name!r} differ only by an ordinal — neighbouring "
            "phases of one development, not one site stored twice"
        )
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


def _surviving(project_id: int, folded: dict[int, int]) -> int:
    """Follow a row through the merges this run has already made.

    A chain rather than a lookup: three rows for one campus can be folded in two
    steps, and the second step's survivor is what a third pair must be asked about.
    """
    seen: set[int] = set()
    while project_id in folded and project_id not in seen:
        seen.add(project_id)
        project_id = folded[project_id]
    return project_id


def resolve_one(
    session: Session,
    pair: Any,
    *,
    extractor,
    allow_merge: bool = False,
    ask=None,
    folded: dict[int, int] | None = None,
) -> Decision:
    """Decide one suspected pair and carry the decision out.

    `ask` is the person at the keyboard, called with (a, b, pair) and returning
    "same", "different", "skip", or None to hand the question to the model. A
    person's answer is trusted for a merge without the confidence floor — that is
    what `--merge` behind a keyboard means — and is recorded as `operator`.

    `folded` maps a row this run has already merged to the row that survived it,
    which is what lets a group of more than two settle in one pass. See
    :func:`resolve`.
    """
    from tracker import pairs as pairs_mod

    folded = folded if folded is not None else {}
    a_id, b_id = _surviving(pair.a_id, folded), _surviving(pair.b_id, folded)
    label = f"#{pair.a_id} {pair.a_company} — {pair.a_name} / #{pair.b_id} {pair.b_company} — {pair.b_name}"
    got = Decision(a_id=pair.a_id, b_id=pair.b_id, label=label)
    if a_id == b_id:
        got.detail = f"already one row — both sides are now #{a_id}"
        return got
    a = session.get(Project, a_id)
    b = session.get(Project, b_id)
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
        # The evidence classes travel with the reason. `duplicates parked` is read
        # months later by somebody deciding whether to reopen the question, and
        # "these are different sites" reads differently when what raised the pair
        # was a shared tranche key than when it was a word.
        classes = "+".join(pair.kinds) or "locality"
        pairs_mod.park(session, [a.id, b.id], reason=f"{judged.reason} [{classes}]", by=by)
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

    keep, gone = survivor(a, b)
    result = merge_projects(session, keep.id, [gone.id])
    record_decision(
        keep,
        "duplicate",
        f"folded #{gone.id} into this row",
        by=by,
        detail=judged.reason,
    )
    # So a later pair naming the row just deleted is asked about the survivor
    # instead of being reported as gone. This is what settles a group of four in
    # one run rather than one merge per run.
    folded[gone.id] = keep.id
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
    in order.

    **A group larger than two used to need one run per merge.** Four rows for one
    campus produce six pairs, and the first merge deleted a row the other five
    named, so they reported "one of the rows is gone" and the operator ran the
    command again. The live database has eight groups of three and two of four —
    among them the Ashburn group where RagingWire and NTT hold `va-4`, `va-5` and
    `va-6` under four names. `folded` carries this run's merges forward, so a later
    pair is asked about the surviving row and the whole group settles in one pass.
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
    folded: dict[int, int] = {}
    out: list[Decision] = []
    for pair in found[:limit]:
        out.append(
            resolve_one(
                session,
                pair,
                extractor=extractor,
                allow_merge=allow_merge,
                ask=ask,
                folded=folded,
            )
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
