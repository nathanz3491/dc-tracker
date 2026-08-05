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
import re
from collections.abc import Iterator
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


def _remember(overview: Overview, *, key: str | None = None) -> None:
    if len(_cache) >= CACHE_SIZE:
        _cache.pop(next(iter(_cache)))
    _cache[key if key is not None else f"{overview.project_id}"] = overview


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


def write(project: Project, *, extractor, prompt_name: str = "overview-v2") -> Overview | None:
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


#: The model finishing the briefing and then carrying on.
#:
#: Every model is asked to end with `[[END]]`. Some honour it, some do not, and
#: `M2-her` — the one MiniMax model that does not think, and so the fastest by a
#: wide margin — reliably writes a good answer and then keeps talking: repeating
#: itself under headings like "Final answer (last round)", narrating its own word
#: count ("Total word count: **75** (markdown consumed)"), emitting stray
#: brackets. The API's own `stop` parameter is accepted and ignored, so this is
#: the only place the tap can be turned off.
#:
#: Cutting the *stream* rather than the finished text is the point: closing the
#: connection early is what turns a 17s reply into a 3.5s one, because the tokens
#: after the answer are never waited for.
RUNAWAY: Final = re.compile(
    # `\[\[` rather than the full sentinel: the model sometimes emits the opening
    # brackets and then stops, or starts spelling it differently, and a bare
    # "[[" rendered at the end of a card is the tell that it was cut badly.
    r"\[\["
    r"|^\s*\]\s*$"
    r"|total word count"
    r"|final answer"
    r"|one last time"
    r"|^\s*(here('s| is) (the|another)|revised|shorter version|let me know)",
    re.IGNORECASE | re.MULTILINE,
)


def stream(project: Project, *, extractor, prompt_name: str = "overview-v2") -> Iterator[str]:
    """The same briefing, yielded as it is written.

    The panel generates on open rather than on a click, so the wait is no longer
    something the reader chose to start — which makes it the whole experience of
    opening a row. Streaming turns a blank card into something legible after a
    second or so.

    Caches on success exactly like `write`, so reopening the row is free. A stream
    that dies partway is *not* cached: half a briefing that stops mid-sentence
    would otherwise be served forever as this row's reading.
    """
    from tracker.llm import LLMError
    from tracker.prompts import load_prompt

    if not hasattr(extractor, "stream"):
        written = write(project, extractor=extractor, prompt_name=prompt_name)
        if written is not None:
            yield written.text
        return

    prompt = load_prompt(prompt_name)
    sent = ""
    try:
        for piece in extractor.stream(
            system=prompt.system,
            user=prompt.render_user(**build_context(project)),
            max_tokens=MAX_TOKENS,
        ):
            candidate = sent + piece
            end = RUNAWAY.search(candidate)
            if end is None:
                sent = candidate
                yield piece
                continue
            # Emit only the part before the model started over, then stop reading.
            # Abandoning the generator closes the HTTP stream, which is where the
            # time is saved.
            tail = candidate[len(sent) : end.start()]
            if tail:
                yield tail
            sent = candidate[: end.start()]
            log.debug(
                "overview for project %s ran past its answer; cut at the sentinel", project.id
            )
            break
    except LLMError as exc:
        log.warning("overview stream for project %s failed: %s", project.id, exc)
        return

    text = sent.strip()
    if len(text) < 40:
        log.warning("overview for project %s came back empty or truncated", project.id)
        return
    _remember(
        Overview(
            project_id=project.id,
            text=text,
            model=getattr(extractor, "model", "unknown"),
            fingerprint=fingerprint(project),
        )
    )


def _strip_reasoning(text: str) -> str:
    """Drop a reasoning model's `<think>` block.

    `parse_json_object` does this on the JSON paths, but this prompt returns prose
    and never goes near a JSON parser — so without it the drawer would render the
    model's private deliberation as the briefing.
    """
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)


# --- One buyer's capex position, read the same way -------------------------------
#
# The capex table's hover card. Same contract as the project briefing — never
# stored, cached by content, cut at the sentinel — over a different subject: one
# buyer's whole position rather than one row. The subject is derived (a rollup),
# so the fingerprint hashes the position's own figures plus the `updated_at` of
# every project behind it: any row moving rewrites the reading.


def position_fingerprint(position: Any, projects: list[Project]) -> str:
    parts = [
        position.key or "unattributed",
        str(position.projects),
        str(position.mw_planned),
        str(position.mw_built),
        str(position.investment_usd),
        str(position.investment_excluded_usd),
        str(position.mw_duplicate_skipped),
    ]
    parts += sorted(f"{p.id}:{p.updated_at}" for p in projects)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def cached_position(position: Any, projects: list[Project]) -> Overview | None:
    found = _cache.get(f"pos:{position.key or 'unattributed'}")
    if found and found.fingerprint == position_fingerprint(position, projects):
        return found
    return None


#: A site's share of its buyer, already in words.
#:
#: The fast model reaches for "half" and "a third" unprompted — it is a dialogue
#: model and that is how people talk — and it gets them wrong: 842 of 3,499 MW
#: came out as "more than a third". Banning the words outright made the briefings
#: stilted and the ban leaked anyway. So the phrase is computed here and the
#: prompt says to copy it, which turns an arithmetic problem into a quoting one.
_SHARE_WORDS: Final[tuple[tuple[float, str], ...]] = (
    (0.95, "effectively all"),
    (0.75, "most"),
    (0.55, "more than half"),
    (0.45, "about half"),
    (0.28, "about a third"),
    (0.20, "about a quarter"),
    (0.10, "a modest share"),
    (0.0, "a small share"),
)


def _share_phrase(fraction: float) -> str:
    for floor, words in _SHARE_WORDS:
        if fraction >= floor:
            return words
    return "a small share"


def build_position_context(position: Any, projects: list[Project]) -> dict[str, str]:
    """One line per site, plus the totals the table already shows.

    **Every derivation the model would otherwise invent is done here and
    labelled.** Measured: given "4,500 MW planned, 1,200 MW running, expected
    online 2026" the fast model subtracts, calls the remainder "3,300 MW due
    mid-year", and has thereby published a schedule no source stated. Adding a
    prose rule against it did not stop it — the gap was simply too inviting. So
    the remainder is computed here, and stated together with the fact that
    nothing dates it. Same for each site's share of the position, which the model
    was dividing out by hand.
    """

    def money(value: int) -> str:
        return f"${value / 1e9:.1f}B" if value else "none"

    total = position.mw_planned or 0.0
    lines = []
    for p in sorted(projects, key=lambda p: -(p.mw_planned or 0)):
        if not p.mw_planned:
            capacity = "capacity: NOT CITED — nobody has said how big this is"
        else:
            capacity = f"capacity: {p.mw_planned:g} MW planned"
            if total:
                capacity += f" — {_share_phrase(p.mw_planned / total)} of this buyer's capacity"
            if p.mw_built:
                capacity += f"; {p.mw_built:g} MW of it running"
                remainder = p.mw_planned - p.mw_built
                if remainder > 0:
                    capacity += f"; the remaining {remainder:g} MW is unbuilt and NOTHING DATES IT"
            else:
                capacity += "; none of it running yet"
        online = (
            f"this campus's own stated online date: {p.expected_online}"
            if p.expected_online
            else "no online date stated for this campus"
        )
        money_line = f"; investment {money(p.investment_usd)}" if p.investment_usd else ""
        lines.append(
            f"  #{p.id} {p.company} — {p.name} ({p.city or p.county or 'unknown'}, {p.state})\n"
            f"      {capacity}\n"
            f"      phase {p.phase}; {online}{money_line}; confidence {p.confidence}/3"
        )

    unsized = sum(1 for p in projects if not p.mw_planned)
    shape = [f"{len(projects)} site(s) counted"]
    if unsized:
        shape.append(f"{unsized} of them with no cited capacity at all")
    operators = {p.company for p in projects}
    if len(operators) > 1:
        shape.append(f"{len(operators)} different operators build them")

    skipped = "none"
    if position.duplicate_rows_skipped:
        skipped = (
            f"{position.duplicate_rows_skipped} row(s) holding "
            f"{position.mw_duplicate_skipped:g} MW look like duplicates of campuses "
            "already counted, and were set aside"
        )

    return {
        "name": position.name,
        "projects": str(position.projects),
        "self_built": str(position.self_built),
        "mw_planned": f"{position.mw_planned:g}",
        "mw_built": f"{position.mw_built:g}",
        "investment_usd": money(position.investment_usd),
        "investment_excluded": money(position.investment_excluded_usd),
        "skipped": skipped,
        "shape": "; ".join(shape),
        "sites": "\n".join(lines) or "  (none)",
        "today": str(_today()),
    }


def stream_position(
    position: Any, projects: list[Project], *, extractor, prompt_name: str = "capex-overview-v1"
) -> Iterator[str]:
    """One buyer's briefing, yielded as it is written. Same rules as `stream`."""
    from tracker.llm import LLMError
    from tracker.prompts import load_prompt

    prompt = load_prompt(prompt_name)
    context = build_position_context(position, projects)

    if not hasattr(extractor, "stream"):
        try:
            reply = extractor.complete(
                system=prompt.system, user=prompt.render_user(**context), max_tokens=MAX_TOKENS
            )
        except LLMError as exc:
            log.warning("position briefing for %r failed: %s", position.name, exc)
            return
        text = _strip_reasoning(reply.text or "")
        cut = RUNAWAY.search(text)
        text = (text[: cut.start()] if cut else text).strip()
        if len(text) >= 40:
            yield text
            _remember(
                Overview(
                    project_id=0,
                    text=text,
                    model=reply.model,
                    fingerprint=position_fingerprint(position, projects),
                ),
                key=f"pos:{position.key or 'unattributed'}",
            )
        return

    sent = ""
    try:
        for piece in extractor.stream(
            system=prompt.system,
            user=prompt.render_user(**context),
            max_tokens=MAX_TOKENS,
        ):
            candidate = sent + piece
            end = RUNAWAY.search(candidate)
            if end is None:
                sent = candidate
                yield piece
                continue
            tail = candidate[len(sent) : end.start()]
            if tail:
                yield tail
            sent = candidate[: end.start()]
            break
    except LLMError as exc:
        log.warning("position briefing for %r failed: %s", position.name, exc)
        return

    text = sent.strip()
    if len(text) < 40:
        log.warning("position briefing for %r came back empty or truncated", position.name)
        return
    _remember(
        Overview(
            project_id=0,
            text=text,
            model=getattr(extractor, "model", "unknown"),
            fingerprint=position_fingerprint(position, projects),
        ),
        key=f"pos:{position.key or 'unattributed'}",
    )


__all__ = [
    "CACHE_SIZE",
    "MAX_TOKENS",
    "RUNAWAY",
    "Overview",
    "build_context",
    "build_position_context",
    "cached",
    "cached_position",
    "fingerprint",
    "position_fingerprint",
    "stream",
    "stream_position",
    "write",
]
