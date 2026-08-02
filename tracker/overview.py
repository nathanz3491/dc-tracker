"""A written briefing on one project, for the console's detail drawer.

Everything else the drawer shows is a value with a citation behind it. This is the
one panel that is a *reading* of those values rather than one of them, and the
whole design problem is making that difference impossible to miss.

Three things follow from it.

**It is never stored as a fact.** The text is not written to `project`, does not
become a `source`, and cannot move `confidence`. It is generated on request and
cached by content, so a reader who reloads gets the same words without paying
twice, and a reader whose data changed gets new ones.

**It is labelled in the interface, not just here.** `gaps.py` exists because a
model's answer is not a fact; a paragraph of fluent prose in the same drawer as
quoted evidence is the easiest place in the whole product to blur that, so the
panel says what it is and stays visually separate from the cited values.

**It is asked to say when it does not know.** Most rows are thin. A briefing that
pads a two-field project with sector commentary reads exactly like knowledge, and
that is worse than no briefing — see the prompt, where this is rule two.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Final

from tracker.models import Project
from tracker.vocab import TRACKED_FIELDS

log = logging.getLogger(__name__)

#: Room for four short paragraphs plus a reasoning model's thinking. Truncation
#: mid-sentence is worse than no briefing, and the reasoning models used here
#: spend most of their budget before the first visible word.
MAX_TOKENS: Final = 4096

#: How many briefings to keep in memory. One per project is the natural size and
#: the console serves one operator; this is a bound against a runaway, not a
#: tuned cache.
CACHE_SIZE: Final = 256

_cache: dict[str, Overview] = {}


@dataclass(frozen=True)
class Overview:
    project_id: int
    text: str
    model: str
    #: Hash of the data the briefing was written from. Changes when the row does,
    #: which is what makes caching safe: a stale reading of superseded evidence is
    #: exactly the failure this panel could produce and never notice.
    fingerprint: str

    def as_json(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "text": self.text,
            "model": self.model,
            "fingerprint": self.fingerprint,
        }


def fingerprint(project: Project) -> str:
    """Everything the briefing depends on, hashed.

    Deliberately includes the sources and the milestones, not just the fields: a
    row can gain a citation that changes how trustworthy it is without any value
    moving, and paragraph four is about exactly that.
    """
    parts = [str(getattr(project, name, None)) for name in TRACKED_FIELDS]
    parts += sorted(f"{e.event_type}:{e.event_date}" for e in project.events or ())
    parts += sorted(f"{r.category}:{r.status}" for r in project.risks or ())
    parts += sorted(s.url for s in project.sources or ())
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def cached(project: Project) -> Overview | None:
    """The briefing already written for this exact state of the row, if any."""
    found = _cache.get(f"{project.id}")
    return found if found and found.fingerprint == fingerprint(project) else None


def _remember(overview: Overview) -> None:
    if len(_cache) >= CACHE_SIZE:
        _cache.pop(next(iter(_cache)))
    _cache[f"{overview.project_id}"] = overview


def build_context(project: Project) -> dict[str, str]:
    """Everything the model may reason from, and nothing else.

    Includes the gaps and the provenance tiers, because paragraph four is about
    how much to trust the row and it cannot be written from the values alone. A
    briefing that cannot see which numbers are 待确认 will describe a guess and a
    quote in the same confident voice.
    """
    from tracker.gaps import FILLED, NOT_APPLICABLE, for_project, provenance
    from tracker.tracks import TRACK_LABELS, standing

    def show(value: Any) -> str:
        return "not recorded" if value is None else str(value)

    prov_lines: list[str] = []
    for name in TRACKED_FIELDS:
        if getattr(project, name, None) is None:
            continue
        record = provenance(project, name)
        if record is None:
            continue
        quote = (record.quote or "").strip().replace("\n", " ")
        line = f"  {name}: {record.tier}"
        if quote:
            line += f' — "{quote[:200]}"'
        prov_lines.append(line)

    stand = standing(project.id, list(project.events or ()), list(project.risks or ()))
    track_lines = []
    for state in stand.tracks:
        reached = ", ".join(state.reached) or "nothing reached"
        nxt = f"; next would be {state.next_milestone}" if state.next_milestone else ""
        blocked = f"; blocked by {', '.join(state.blockers)}" if state.blockers else ""
        track_lines.append(f"  {TRACK_LABELS[state.track]}: {reached}{nxt}{blocked}")

    milestones = sorted({(e.event_type, str(e.event_date)) for e in project.events or ()})
    risks = [
        f"  - {r.category} ({r.severity}): {r.summary}"
        for r in project.risks or ()
        if getattr(r, "status", "open") == "open"
    ]
    gaps = []
    for state in for_project(project, TRACKED_FIELDS):
        if state.status == FILLED:
            continue
        why = " — " + state.reason if state.reason else ""
        kind = "cannot apply here" if state.status == NOT_APPLICABLE else "nobody has said"
        gaps.append(f"  {state.field}: {kind}{why}")
    sources = [
        f"  - {s.source_type} ({str(s.fetched_at)[:10]}) {s.url}" for s in project.sources or ()
    ]

    return {
        "project_id": str(project.id),
        "name": show(project.name),
        "company": show(project.company),
        "customer": show(project.customer),
        "location": f"{project.city or project.county or 'unknown'}, {project.state}",
        "mw_planned": show(project.mw_planned),
        "mw_built": show(project.mw_built),
        "investment_usd": show(project.investment_usd),
        "phase": show(project.phase),
        "first_announced": show(project.first_announced),
        "expected_online": show(project.expected_online),
        "confidence": str(project.confidence),
        "provenance": "\n".join(prov_lines) or "  (nothing populated)",
        "tracks": "\n".join(track_lines),
        "milestones": "\n".join(f"  - {t} on {d}" for t, d in milestones) or "  (none recorded)",
        "risks": "\n".join(risks) or "  (none recorded)",
        "gaps": "\n".join(gaps) or "  (nothing missing)",
        "sources": "\n".join(sources) or "  (none)",
        "today": str(_today()),
    }


def _today():
    from tracker.models import utcnow

    return utcnow().date()


def write(project: Project, *, extractor, prompt_name: str = "overview-v1") -> Overview | None:
    """Generate the briefing. Costs one call. None when it could not be written.

    Returns None rather than a placeholder: an empty panel is honest and a
    sentence apologising for itself is clutter in a drawer that already has a lot
    to say.
    """
    from tracker.llm import LLMError
    from tracker.prompts import load_prompt

    prompt = load_prompt(prompt_name)
    try:
        reply = extractor.complete(
            system=prompt.system,
            user=prompt.render_user(**build_context(project)),
            max_tokens=MAX_TOKENS,
        )
    except LLMError as exc:
        log.warning("could not write an overview for project %s: %s", project.id, exc)
        return None

    text = _strip_reasoning(reply.text or "").strip()
    if len(text) < 40:
        log.warning("overview for project %s came back empty or truncated", project.id)
        return None

    overview = Overview(
        project_id=project.id,
        text=text,
        model=reply.model,
        fingerprint=fingerprint(project),
    )
    _remember(overview)
    return overview


def _strip_reasoning(text: str) -> str:
    """Drop a reasoning model's `<think>` block.

    `parse_json_object` does this on the JSON paths, but this prompt returns prose
    and never goes near a JSON parser — so without it the drawer would render the
    model's private deliberation as the briefing.
    """
    import re

    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)


__all__ = [
    "CACHE_SIZE",
    "MAX_TOKENS",
    "Overview",
    "build_context",
    "cached",
    "fingerprint",
    "write",
]
