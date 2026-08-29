"""Point the tracker at one named data center: find it, or go and build it.

Everything else here works in batches — poll the feeds, read what turns up, see
what accumulates. This is the other direction: you have heard a name and want
that one campus, now.

Two answers are possible and they lead to opposite work.

**We already have it**, under some spelling. "Stargate Abilene", "Crusoe Abilene
Data Center" and "Stargate (星际之门) - Abilene" are one building, and adding a
fourth row for it would add a row the capex table has to skip and disclose, in
the way `tracker duplicates` exists to catch. So the first job is matching, and
matching a name against 224
rows is a judgement — "Project Camellia" and "Camellia Data Center" probably are
the same and "Stargate Milam County" and "Stargate Lordstown" definitely are not,
and no string metric gets both right.

**We do not**, and then the job is to go and read about it specifically rather
than waiting for a feed to mention it.

**How the model is kept honest.** It never sees a free-text box. A deterministic
prefilter picks a shortlist, the model may answer only with an id from that
shortlist or "none", and a low-confidence answer is treated as "none" — which
routes to building a new profile, the recoverable mistake. Getting that backwards
and merging two real campuses is the expensive one, and `tracker merge` exists
because it cannot be undone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.dedup import company_key
from tracker.models import Project

log = logging.getLogger(__name__)

#: How many rows the model is asked to choose between. Enough that the right one
#: is almost always present, few enough that the prompt stays readable and the
#: model is not skimming.
SHORTLIST: Final = 12

#: Below this the answer is treated as "no match", which routes to building a new
#: profile. Deliberately high: a wrong match silently folds a real campus into
#: another row's history, and a wrong miss creates a duplicate that
#: `tracker duplicates` already finds and `tracker merge` already fixes.
MIN_CONFIDENCE: Final = 0.7

#: Words that carry no identity. Every other data center is a "data center".
_NOISE: Final[frozenset[str]] = frozenset(
    {
        "data",
        "center",
        "centre",
        "datacenter",
        "datacentre",
        "campus",
        "facility",
        "site",
        "project",
        "the",
        "and",
        "of",
        "at",
        "in",
        "phase",
        "expansion",
        "llc",
        "inc",
        "corp",
        "co",
        "dc",
        "building",
        "hall",
        "park",
    }
)


def tokens(text: str) -> frozenset[str]:
    """Identity-bearing words, lowercased. Digits kept — "COL4" is a name."""
    cleaned = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return frozenset(w for w in cleaned.split() if w and w not in _NOISE and len(w) > 1)


@dataclass(frozen=True)
class Candidate:
    project_id: int
    label: str
    score: float


def shortlist(session: Session, query: str, *, limit: int = SHORTLIST) -> list[Candidate]:
    """Rows worth asking about, best first.

    A cheap overlap score, not a decision. Its only job is to make sure the right
    row is in the list; choosing between them is what the model is for. Company
    names count too, because people name a campus after its operator as often as
    after its site.
    """
    wanted = tokens(query)
    if not wanted:
        return []

    scored: list[Candidate] = []
    for project in session.scalars(select(Project)).all():
        have = tokens(
            f"{project.name} {project.company} {project.city or ''} {project.county or ''}"
        )
        shared = wanted & have
        if not shared:
            continue
        # Jaccard-ish, but biased towards covering the query: somebody typing
        # "Stargate Abilene" should match a row called
        # "Crusoe Stargate Abilene Data Center Campus" despite the extra words.
        score = len(shared) / len(wanted)
        if company_key(project.company) and company_key(project.company) in wanted:
            score += 0.25
        location = ", ".join(x for x in (project.city or project.county, project.state) if x)
        scored.append(
            Candidate(
                project_id=project.id,
                label=f"{project.company} — {project.name} ({location})",
                score=round(score, 3),
            )
        )
    scored.sort(key=lambda c: (-c.score, c.project_id))
    return scored[:limit]


@dataclass(frozen=True)
class Match:
    """What the model concluded about a name."""

    project_id: int | None
    confidence: float
    reason: str
    #: Set when an answer was discarded, and why.
    rejected: str | None = None

    @property
    def matched(self) -> bool:
        return self.project_id is not None


def identify(query: str, candidates: list[Candidate], *, extractor, prompt_name: str = "point-v1"):
    """Ask which shortlisted row is the same campus, if any.

    Returns a `Match` with `project_id=None` for "build it fresh", which is also
    what every failure returns: an unreachable model, an unparseable reply and a
    hedged answer all mean "do not fold this into an existing row".
    """
    from tracker.llm import LLMError
    from tracker.prompts import load_prompt

    if not candidates:
        return Match(None, 1.0, "nothing in the database shares a distinctive word with that name")

    prompt = load_prompt(prompt_name)
    listing = "\n".join(f"  {c.project_id}  {c.label}" for c in candidates)
    try:
        reply = extractor.complete(
            system=prompt.system,
            user=prompt.render_user(query=query, candidates=listing),
            max_tokens=2048,
        )
    except LLMError as exc:
        log.warning("could not identify %r: %s", query, exc)
        return Match(None, 0.0, "", rejected=f"call failed: {exc}")
    return _read_match(reply.text, candidates)


def _read_match(text: str, candidates: list[Candidate]) -> Match:
    """Validate one reply into a `Match`.

    Shared by both entry points so the *rails* cannot diverge from the prompt that
    happened to be used: an id must come off the shortlist, and the confidence
    floor is enforced here rather than trusted to the model. Every rejection
    returns no match, which builds a new row.
    """
    from tracker.llm import LLMJsonError, parse_json_object

    try:
        payload = parse_json_object(text)
    except (LLMJsonError, ValueError):
        return Match(None, 0.0, "", rejected="unusable reply")

    reason = str(payload.get("reason") or "").strip()
    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    raw = payload.get("project_id")
    if raw in (None, "", "none", "None"):
        return Match(None, confidence, reason)
    try:
        chosen = int(raw)
    except (TypeError, ValueError):
        return Match(None, confidence, reason, rejected=f"{raw!r} is not an id")

    if chosen not in {c.project_id for c in candidates}:
        # The one failure mode worth naming: a model that returns a plausible id
        # it was never offered would silently attach a name to an unrelated row.
        return Match(None, confidence, reason, rejected=f"#{chosen} was not on the shortlist")
    if confidence < MIN_CONFIDENCE:
        return Match(
            None,
            confidence,
            reason,
            rejected=f"confidence {confidence:.2f} below the {MIN_CONFIDENCE} floor for a match",
        )
    return Match(chosen, confidence, reason)


@dataclass(frozen=True)
class Identity:
    """What one article turned out to be about, as the write path sees it.

    The whole reason this exists: identification used to run on a *typed name*,
    before anything was fetched, and its answer was then thrown away — `--url`
    read the links and let the dedup key decide alone. So an article naming a town
    could not be recognised as describing a campus already stored under its county,
    and the run minted a second row for one site.

    An extracted record knows the operator and the locality, which is the evidence
    that settles exactly that case. Same shape as the shortlist query, so one
    string feeds both the prefilter and the prompt.
    """

    company: str
    name: str
    city: str | None = None
    county: str | None = None
    state: str | None = None

    @classmethod
    def from_record(cls, rec) -> Identity:
        """Read an `IngestRecord`'s project payload. Absent fields stay None."""
        payload = getattr(rec, "project", None) or {}
        return cls(
            company=str(payload.get("company") or "").strip(),
            name=str(payload.get("name") or "").strip(),
            city=(payload.get("city") or None),
            county=(payload.get("county") or None),
            state=(payload.get("state") or None),
        )

    @property
    def locality(self) -> str | None:
        return self.city or self.county

    def as_query(self) -> str:
        """The prefilter's input: every identifying word this article supplied.

        Deliberately more than the name. `shortlist` scores token overlap, so
        handing it the operator and the locality is what puts a row stored under
        the *county* on a list built from an article naming the *town* — the
        candidate a name-only query cannot reach.
        """
        parts = [self.company, self.name, self.city or "", self.county or "", self.state or ""]
        return " ".join(p for p in parts if p)

    def as_block(self) -> str:
        """The prompt's input, one labelled fact per line.

        Labelled rather than run together because the model is being asked to
        weigh *place* above *name*, and a blob of words hides which is which.
        """
        rows = [
            ("operator", self.company or "—"),
            ("project name", self.name or "—"),
            ("city", self.city or "—"),
            ("county", self.county or "—"),
            ("state", self.state or "—"),
        ]
        return "\n".join(f"  {label:<13}{value}" for label, value in rows)


def identify_extracted(
    identity: Identity,
    candidates: list[Candidate],
    *,
    extractor,
    prompt_name: str = "point-v2",
) -> Match:
    """Same question as :func:`identify`, asked from what an article actually said.

    Separate entry point rather than an argument on `identify`, because the two
    take different prompts and mean different things: one is "somebody typed this
    name", the other is "a source asserts this operator at this place". Collapsing
    them would put a name-shaped prompt in front of location-shaped evidence.

    Every failure returns no match, which routes to the ordinary write path and a
    new row — the recoverable mistake. See the asymmetry note at the top of this
    module.
    """
    if not candidates:
        return Match(None, 1.0, "nothing in the database shares a distinctive word with it")

    from tracker.llm import LLMError
    from tracker.prompts import load_prompt

    prompt = load_prompt(prompt_name)
    listing = "\n".join(f"  {c.project_id}  {c.label}" for c in candidates)
    try:
        reply = extractor.complete(
            system=prompt.system,
            user=prompt.render_user(identity=identity.as_block(), candidates=listing),
            max_tokens=2048,
        )
    except LLMError as exc:
        log.warning("could not identify %r: %s", identity.as_query(), exc)
        return Match(None, 0.0, "", rejected=f"call failed: {exc}")
    return _read_match(reply.text, candidates)


def queries_for(name: str) -> list[str]:
    """Searches aimed at one campus rather than at the sector.

    Hand-built rather than asked for, unlike `search --from-llm`. The name is
    already the specific thing being looked for, so a model would be paraphrasing
    it — and its usual job, steering away from projects already tracked, is
    exactly wrong here.
    """
    base = name.strip()
    return [
        f'"{base}" data center',
        f'"{base}" megawatt OR MW capacity',
        f'"{base}" construction OR permit OR interconnection',
    ]


__all__ = [
    "MIN_CONFIDENCE",
    "SHORTLIST",
    "Candidate",
    "Identity",
    "Match",
    "identify",
    "identify_extracted",
    "queries_for",
    "shortlist",
    "tokens",
]
