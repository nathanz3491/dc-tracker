"""Settling a contested field by reading every source that spoke to it.

**What this is for.** Every value in the database was extracted from *one article
in isolation*, and the disagreements between articles are then settled by a sort:
`(confirmed, weight, recency, url)`. That works, and it is the right default —
but it cannot tell a superseded figure from a rival one, and it has never
*compared* two contradicting sentences, because no model has ever seen two of them
at once. Hyperion (#10) held Meta's 2024 $10B over its 2026 $50B for exactly that
reason: same publisher, same weight, both quote-backed, and crawl order decided it.

Four decisions shape this module, and three of them depart from the obvious design.

**One call per disagreed field, not per article.** "Show the model all the
sources" is right in spirit and impossible literally — #10 has 61 sources against
a 24,000-character input limit. What fits, and what actually matters, is every
*claim about the field in dispute*: its value, its verified quote, who published
it, when, and how much weight its type carries. Those group down further, because
44 claims on #10's investment figure are 5 distinct values.

**The model chooses; it never authors.** The options are values already claimed by
a quote-backed citation, and the quote shown with each is the one already stored.
So a fabricated sentence cannot enter the database by this path at all — not
because a gate catches it, but because there is nowhere for it to go. That is
stronger than re-running the evidence gate on a returned quote, and much simpler.

**Refusing is a first-class answer, and the flowchart this came from has no box
for it.** Two credible publishers stating two figures with nothing to separate
them is not a coin toss. The house rule is: never silently pick between two
quote-backed values that disagree — refuse and flag. A refusal writes nothing,
which leaves the disagreement disclosed in the project's notes where
`upsert._conflict_notes` already puts it.

**Two calls per field, hard.** One to decide, one adversarial call to knock the
answer down. The flowchart's "go round again" arrow has no limit on it, and an
unbounded argument is unbounded spend; a refusal carrying the objection is a
better outcome than a third call arguing with itself.

**Proposes by default.** `--apply` is what writes, like `logic resolve` and
`tracker duplicates` before it. And what it writes is not the field: it marks the
losing claims `superseded` on their own source rows, and lets the ordinary merge
re-derive the value. That is the mechanism `upsert.DECIDED_REASONS` exists for,
and it means nothing here is a second write path — the citations still say what
they said, and the row still equals what its citations imply.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from sqlalchemy.orm import Session

from tracker.models import Project, Source
from tracker.vocab import TRACKED_FIELDS, WRITABLE_FIELDS

log = logging.getLogger(__name__)

#: Hard ceiling on model calls for one field: decide, then try to refute.
MAX_CALLS_PER_FIELD = 2

#: Below this the answer is discarded and recorded as a refusal. Same floor as
#: `logic.decide`, for the same reason: a hedged answer written into the database
#: is worse than no answer, because nothing downstream can see the hedge.
MIN_CONFIDENCE = 0.6

#: The reason code written on a claim this pass ruled against. One of
#: `vocab.UNCONFIRMED_REASONS`, and the only one that records a *decision* rather
#: than a measurement — which is why it survives a re-crawl of the same article.
SUPERSEDED = "superseded"

#: What each contested field means, so the prompt does not have to guess. Only the
#: ones a disagreement actually arises on; anything else falls back to its name.
FIELD_NOTES: dict[str, str] = {
    "mw_planned": "the data center's own planned IT load in megawatts, never a "
    "utility's generating capacity and never a company-wide programme total",
    "mw_built": "the IT load actually energised and serving today",
    "investment_usd": "total announced capital for THIS campus, in US dollars",
    "customer": "the company that will occupy or lease the capacity, which is "
    "often not the company building it",
    "expected_online": "when the first or next capacity is expected to serve",
    "first_announced": "when this project was first made public",
    "phase": "how far the campus has actually got",
}

#: Option keys. `r` is missing on purpose — it is the refusal key, and an 18th
#: option lettered `r` would turn a refusal into a silent pick of that option,
#: which is the one outcome this module exists to prevent. The live maximum is 6
#: options, so this never bites; a latent collision of that shape is not worth
#: leaving in.
_KEYS = "abcdefghijklmnopqstuvwxyz"


@dataclass(frozen=True)
class Option:
    """One distinct value, and every citation that states it."""

    key: str
    value: Any
    quote: str
    urls: tuple[str, ...]
    source_type: str
    weight: int
    #: Publication date as a string, or the crawl date labelled as such. Never a
    #: bare date that could be either — which is the whole reason this pass exists.
    when: str

    def render(self) -> str:
        rest = f" (+{len(self.urls) - 1} more citation(s))" if len(self.urls) > 1 else ""
        return (
            f"  {self.key})  {self.value}\n"
            f"      {self.source_type} (weight {self.weight}), {self.when}{rest}\n"
            f'      {self.urls[0]}\n'
            f'      "{self.quote}"'
        )


@dataclass(frozen=True)
class Dispute:
    """One field of one project that quote-backed sources disagree about."""

    project_id: int
    project_name: str
    field: str
    stored: Any
    options: tuple[Option, ...]

    @property
    def claims(self) -> int:
        return sum(len(o.urls) for o in self.options)


@dataclass
class Outcome:
    """What the solver decided about one dispute."""

    dispute: Dispute
    #: `resolved` · `refused` · `error`
    verdict: str
    chosen: Option | None = None
    reason: str = ""
    confidence: float = 0.0
    calls: int = 0
    #: False when the answer was refused before any adversarial call was worth
    #: making. Reported, because "nothing challenged it" and "it survived a
    #: challenge" are different degrees of belief.
    checked: bool = False
    #: URLs whose claim this pass ruled against. Empty unless `verdict` is
    #: `resolved`.
    superseded: tuple[str, ...] = ()

    def render(self) -> str:
        head = f"#{self.dispute.project_id} {self.dispute.field}"
        if self.verdict == "resolved" and self.chosen is not None:
            return (
                f"{head}: {self.dispute.stored} → {self.chosen.value} "
                f"({self.confidence:.2f}) — {self.reason}"
            )
        if self.verdict == "refused":
            return f"{head}: refused — {self.reason}"
        return f"{head}: error — {self.reason}"


@dataclass
class SolveReport:
    disputes: int = 0
    resolved: int = 0
    refused: int = 0
    errors: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Source rows marked `superseded`. Zero unless `apply`.
    written: int = 0
    outcomes: list[Outcome] = dc_field(default_factory=list)

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("contested fields", self.disputes),
            ("resolved", self.resolved),
            ("refused", self.refused),
            ("errors", self.errors),
            ("model calls", self.calls),
            ("claims superseded", self.written),
        ]


# --- selection ---------------------------------------------------------------


def _when(claim: Any) -> str:
    """When this claim was published, or the crawl date said to be one.

    The distinction is the point. `fetched_at` is when we happened to visit, which
    is arbitrary with respect to the truth, and a prompt shown a bare date would
    reason about supersession from crawl order — the exact mistake this whole
    change exists to stop.
    """
    published = getattr(claim, "published_at", None)
    if published is not None:
        return f"published {published:%Y-%m-%d}"
    fetched = getattr(claim, "fetched_at", None)
    if fetched is not None:
        return f"publication date unknown; crawled {fetched:%Y-%m-%d}"
    return "no date of any kind"


def disputes(project: Project) -> list[Dispute]:
    """Fields where two or more quote-backed claims genuinely disagree.

    Deliberately narrow. Running this on every field of every project would be
    126 calls to do nothing on 123 of them, which is the objection `logic resolve`
    already answers with its free rule pass — the deterministic rules settle 285
    findings at no cost, and only what survives them is worth a model.

    Four filters, each removing a case a model cannot help with:

    * **Quote-backed only.** A 待确认 claim already loses to a confirmed one by
      rule, in every policy. There is nothing to settle.
    * **Genuinely different**, by `confidence.values_conflict` — the same
      tolerance the conflict disclosures use, so this cannot report a dispute the
      row's own notes do not.
    * **Tracked fields only.** `notes` is assembled and `blocker` is derived from
      the risk rows; neither is merged from claims at all.
    * **Never an identity field.** "Hyperion" against "Richland Parish Data
      Center" is two names for one campus, not two claims about the world, and
      `FILL_ONLY` says plainly that churn there is worse than staleness. Ruling
      against a claim would not even move the value — `resolve` keeps what the row
      already holds — so it is spend with no possible effect. Measured on the live
      database: 174 of 666 contested fields are `name` or `company`.
    """
    from tracker.confidence import values_conflict
    from tracker.upsert import DERIVED_FIELDS, FIELD_POLICY, Policy, claims_by_field

    out: list[Dispute] = []
    by_field = claims_by_field(list(project.sources))
    for name, claims in sorted(by_field.items()):
        if name in DERIVED_FIELDS or name not in TRACKED_FIELDS:
            continue
        if FIELD_POLICY.get(name) is Policy.FILL_ONLY:
            continue
        backed = [c for c in claims if c.confirmed]
        if len(backed) < 2:
            continue

        # Group to distinct values. 44 claims on one investment figure are 5
        # answers, and showing the model 44 rows would spend the input budget
        # repeating itself.
        grouped: list[list[Any]] = []
        for claim in backed:
            for group in grouped:
                if not values_conflict(group[0].value, claim.value):
                    group.append(claim)
                    break
            else:
                grouped.append([claim])
        if len(grouped) < 2:
            continue

        # Keys are assigned *after* the quote check, so the list the model sees
        # reads a, b, c. Lettering first and then dropping options left gaps —
        # "a, e, f, i" — which is a puzzle to hand a reader for no reason.
        quoted = [(g, _quote_for(project, g[0].url, name)) for g in grouped]
        options = [
            Option(
                key=key,
                value=group[0].value,
                quote=quote,
                urls=tuple(c.url for c in group),
                source_type=group[0].source_type,
                weight=group[0].weight,
                when=_when(group[0]),
            )
            # A claim counted as confirmed whose stored quote cannot be found is
            # not evidence to put in front of a model. Dropping the option is
            # safer than showing a value with no sentence under it.
            for key, (group, quote) in zip(_KEYS, [q for q in quoted if q[1]], strict=False)
        ]
        if len(options) < 2:
            continue
        out.append(
            Dispute(
                project_id=project.id,
                project_name=f"{project.company} — {project.name}",
                field=name,
                stored=getattr(project, name, None),
                options=tuple(options),
            )
        )
    return out


def _quote_for(project: Project, url: str, field: str) -> str | None:
    """The verbatim sentence this citation stored for this field."""
    for source in project.sources:
        if source.url != url or not source.quotes:
            continue
        try:
            quotes = json.loads(source.quotes)
        except (TypeError, ValueError):
            return None
        text = quotes.get(field) if isinstance(quotes, dict) else None
        return str(text).strip() if text else None
    return None


# --- the two calls -----------------------------------------------------------


def solve(dispute: Dispute, *, extractor: Any) -> Outcome:
    """Decide one dispute. At most `MAX_CALLS_PER_FIELD` model calls."""
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    outcome = Outcome(dispute=dispute, verdict="refused", reason="not attempted")
    prompt = load_prompt("resolve-v1")
    user = prompt.render_user(
        project_id=dispute.project_id,
        project_name=dispute.project_name,
        field=dispute.field,
        field_note=FIELD_NOTES.get(dispute.field, dispute.field.replace("_", " ")),
        stored=dispute.stored if dispute.stored is not None else "nothing",
        option_count=len(dispute.options),
        options="\n\n".join(o.render() for o in dispute.options),
    )
    try:
        reply = extractor.complete(system=prompt.system, user=user)
        outcome.calls += 1
        answer = parse_json_object(reply.text)
    except (LLMError, LLMJsonError) as exc:
        outcome.verdict = "error"
        outcome.reason = str(exc)[:200]
        return outcome

    pick = str(answer.get("pick") or "").strip().lower()[:1]
    outcome.reason = str(answer.get("reason") or "").strip()[:400]
    try:
        outcome.confidence = float(answer.get("confidence") or 0.0)
    except (TypeError, ValueError):
        outcome.confidence = 0.0

    chosen = next((o for o in dispute.options if o.key == pick), None)
    if chosen is None:
        # Includes "r" and anything not on the list. Both are refusals, and
        # collapsing them is deliberate: a key nobody offered is not an answer.
        outcome.verdict = "refused"
        outcome.reason = outcome.reason or "the model refused to choose"
        return outcome
    if outcome.confidence < MIN_CONFIDENCE:
        outcome.verdict = "refused"
        outcome.reason = f"{outcome.reason} [discarded at {outcome.confidence:.2f}]"
        return outcome

    if outcome.calls >= MAX_CALLS_PER_FIELD:
        outcome.verdict = "resolved"
        outcome.chosen = chosen
        return outcome

    stands, why = _challenge(dispute, chosen, outcome.reason, extractor=extractor)
    outcome.calls += 1
    outcome.checked = True
    if not stands:
        outcome.verdict = "refused"
        outcome.reason = f"knocked down: {why}"
        return outcome

    outcome.verdict = "resolved"
    outcome.chosen = chosen
    outcome.superseded = tuple(
        url for o in dispute.options if o is not chosen for url in o.urls
    )
    return outcome


def _challenge(dispute: Dispute, chosen: Option, reason: str, *, extractor: Any):
    """The adversarial second call. Returns `(stands, why)`.

    An error here lets the answer stand rather than refusing it. The pick already
    cleared the confidence floor on evidence a person can read; throwing it away
    because a second request timed out would make the outcome depend on the
    network.
    """
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    prompt = load_prompt("resolve-check-v1")
    rivals = [o for o in dispute.options if o is not chosen]
    user = prompt.render_user(
        project_id=dispute.project_id,
        project_name=dispute.project_name,
        field=dispute.field,
        picked=f"{chosen.key}) {chosen.value} — {chosen.when}",
        reason=reason,
        rivals="\n\n".join(o.render() for o in rivals),
    )
    try:
        reply = extractor.complete(system=prompt.system, user=user)
        answer = parse_json_object(reply.text)
    except (LLMError, LLMJsonError) as exc:
        log.warning("re-check failed for #%d %s: %s", dispute.project_id, dispute.field, exc)
        return True, ""
    stands = bool(answer.get("stands", True))
    return stands, str(answer.get("reason") or "").strip()[:400]


# --- writing -----------------------------------------------------------------


def supersede(source: Source, field: str) -> bool:
    """Mark one citation's claim about one field as superseded. Idempotent.

    Two columns move: `unconfirmed_reasons` records *why*, and `unconfirmed_fields`
    is what `claims_by_field` reads to demote the claim out of the merge.

    **`source.fields` is deliberately left alone**, which looks wrong for a second
    and is what the ingest path does. That column means "a verbatim quote supports
    this", and a superseded value still has one — the article really did say $10
    billion, and it was right in 2024. `upsert_record` writes exactly this shape
    when it carries a superseded reason across a re-crawl, so removing the field
    here would make the two paths disagree the moment the same article is read
    again, and would quietly drop a project's confidence on the strength of a
    citation that is still perfectly good evidence of what was announced.

    The claim itself is untouched for the same reason. The article still says what
    it said; it simply stops deciding the merge.
    """
    try:
        reasons = json.loads(source.unconfirmed_reasons or "{}")
    except (TypeError, ValueError):
        reasons = {}
    if not isinstance(reasons, dict):
        reasons = {}
    if reasons.get(field) == SUPERSEDED:
        return False

    reasons[field] = SUPERSEDED
    source.unconfirmed_reasons = json.dumps(reasons, sort_keys=True, ensure_ascii=False)

    unconfirmed = {f.strip() for f in (source.unconfirmed_fields or "").split(",") if f.strip()}
    unconfirmed.add(field)
    # Canonical order, matching `upsert.derive_fields`, so a row written here is
    # byte-identical to one the ingest path would write.
    source.unconfirmed_fields = ",".join(f for f in WRITABLE_FIELDS if f in unconfirmed) or None
    return True


def apply_outcome(session: Session, project: Project, outcome: Outcome) -> int:
    """Write one resolved outcome. Returns citations marked. Nothing else moves.

    The project's field is never assigned here. Marking the losing claims and then
    re-deriving is what keeps the one guarantee this database rests on: every value
    equals what its citations imply. Assigning the field directly would make the
    row a thing somebody typed, and the next `backfill derive` would put it back.
    """
    from tracker.upsert import recompute_from_sources

    if outcome.verdict != "resolved":
        return 0
    losers = set(outcome.superseded)
    marked = sum(1 for s in project.sources if s.url in losers and supersede(s, outcome.dispute.field))
    if marked:
        session.flush()
        recompute_from_sources(session, project)
    return marked


__all__ = [
    "FIELD_NOTES",
    "MAX_CALLS_PER_FIELD",
    "MIN_CONFIDENCE",
    "Dispute",
    "Option",
    "Outcome",
    "SolveReport",
    "apply_outcome",
    "disputes",
    "solve",
    "supersede",
]
