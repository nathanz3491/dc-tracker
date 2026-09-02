"""Filling a row's empty fields by letting a model go and find the source.

The agent-backed half of `enrich`, and it exists because `fields_present` is the
sole thing holding **283 of 437 rows** at T1 — every other T2 condition passes on
all of them. Measured on the live database after a night of settlement work: the
errors are largely gone (`no_errors` failing on 1 row, down from 42) and what is
left is absence. No amount of `logic resolve` or `duplicates resolve` moves those
283, because there is nothing wrong with them; there is something missing.

**Why an agent rather than more of `enrich`'s existing harvest.** `enrich` searches
from a fixed table — `_FIELD_QUERIES` maps `mw_planned` to the literal phrases
"megawatts capacity" and "MW data center campus", anchored on the company and
locality. That is the same fixed-menu shape that capped `logic.decide` at 94 of
526 findings: it can only ask what somebody wrote down in advance. A model looking
at the actual row can see that a campus with a named anchor tenant and no
investment figure should be searched for the tenant's own announcement, and that a
county-level row wants the county board's agenda rather than trade press.

**The fact is stored as a CITATION, never as a column.** This is the same lesson
`triage` is built on, arrived at from the other direction: a project scalar is a
cache of the claim set, so assigning it is undone by the next `backfill derive`.
So the terminal tool does not set a field — it reports *a sentence in a document
at a URL*, and `upsert_record` attaches that as a source whose `claims` carry the
value and whose `quotes` carry the sentence. The merge policy then derives the
field, the evidence gate marks it `reported` rather than `inferred`, and `capex`
will count it. A value the model could not quote is refused outright: this path
must not become `infer` with a search engine bolted on.

`existing_only=True` and `route_to` are both passed, so a run can only ever add
citations to the row it was asked about. An enrich pass that quietly created
projects would be an ingest with no worklist — the trap `upsert_record`'s own
docstring describes.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

log = logging.getLogger(__name__)

#: Fields this path may fill. The identity fields are absent deliberately: they are
#: never overwritten once set, so a citation claiming them changes nothing and
#: would only look like it had. `blocker` is absent because it is derived from the
#: risk rows rather than merged from claims.
FILLABLE_FIELDS: frozenset[str] = frozenset(
    {
        "mw_planned",
        "mw_built",
        "investment_usd",
        "first_announced",
        "expected_online",
        "customer",
        "phase",
        "county",
    }
)

#: A quote shorter than this is not evidence. "250 MW" appears in every article
#: about a 250 MW site and ties the figure to nothing; the gate downstream would
#: refuse it anyway, and refusing here saves the write.
MIN_QUOTE_CHARS: int = 40


@dataclass
class Filled:
    """What one gap-filling run found and what was stored."""

    #: "filled" — at least one citation was attached and the row re-derived.
    #: "nothing" — the model looked and reported that it found nothing.
    #: "unusable" — it answered in a shape that could not be applied.
    #: "error" — the run never reached an answer.
    verdict: str
    note: str = ""
    stored: list[str] = dc_field(default_factory=list)
    refused: list[str] = dc_field(default_factory=list)
    steps: list[str] = dc_field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def acted(self) -> bool:
        return self.verdict == "filled" and bool(self.stored)


def record_facts_tool(gaps: list[str]) -> Any:
    """Terminal tool: report facts found, each with the sentence that states it.

    `gaps` narrows the enum to the fields this row is actually missing, so the
    model cannot spend a run filling something already known — and so the schema
    itself tells it what the job is.
    """
    from tracker.agent import Tool

    return Tool(
        name="record_facts",
        description=(
            "Report one or more facts you found, each with the URL of the document "
            "and the sentence in it that states the fact. Each becomes a citation on "
            "this project; the database derives the field from it. You are not "
            "setting a value — you are supplying evidence. A fact you cannot quote "
            "will be refused, so do not report one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "enum": sorted(gaps),
                                "description": "Which missing field this fact fills.",
                            },
                            "value": {
                                "description": (
                                    "The value. A number for megawatts (MW) and for "
                                    "investment (whole US dollars, so $1.2 billion is "
                                    "1200000000). An ISO date (YYYY-MM-DD) for a date; "
                                    "use the first of the month or year when only that "
                                    "is stated. A string for customer, county, phase."
                                )
                            },
                            "url": {
                                "type": "string",
                                "description": "The document you read the sentence in.",
                            },
                            "quote": {
                                "type": "string",
                                "description": (
                                    "The whole sentence, copied exactly from that "
                                    "document, that states this value for THIS campus. "
                                    f"At least {MIN_QUOTE_CHARS} characters."
                                ),
                            },
                        },
                        "required": ["field", "value", "url", "quote"],
                    },
                },
                "reason": {"type": "string", "description": "One sentence on what you found."},
            },
            "required": ["facts"],
        },
        terminal=True,
    )


def nothing_found_tool() -> Any:
    """Terminal tool: the fields are missing because nobody has published them.

    A real and common answer. Absence is the correct value for a great many of
    these — a site nobody has sized has no megawatt figure, and inventing one is
    the failure this whole database is built to avoid.
    """
    from tracker.agent import Tool

    return Tool(
        name="nothing_found",
        description=(
            "You searched and read, and the missing fields are missing because no "
            "source states them for this campus. Say what you looked at. This is a "
            "correct answer and is recorded as one."
        ),
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
        terminal=True,
    )


SYSTEM = """You find missing facts about US data centre projects, and you cite them.

The row you are given has empty fields. Your job is to find a published document
that states one of them for THIS campus, and report the sentence.

How to work:
1. `show_project` to see what is known and what is empty.
2. `list_sources` — often the answer is already in an article the database holds
   and simply was not extracted. Check before you search.
3. `search_web` for what is still missing. Search for what would actually carry
   it: an operator's own newsroom for a capacity figure, a county planning agenda
   for a permit, a tenant's announcement for a lease, an SEC filing for money.
4. `read_article` a result before you quote it. Never quote a search snippet.
5. `record_facts`, or `nothing_found`.

THE THREE WAYS THIS GOES WRONG, in order of how often:

*Wrong campus.* Operators run many sites and reuse names. A figure for "the
Ashburn campus" is not a figure for this building. Check the city, the county and
the operator against the row before you believe a number.

*Wrong scope.* A "total" in an article about one expansion is usually the whole
campus, and a programme-wide investment figure is not this site's. If the sentence
does not make the scope explicit for this campus, do not report it.

*No quote.* If you cannot copy a sentence that states the value, you have not
found the fact — you have found something that implies it. Report
`nothing_found`. Absence is the right answer far more often than a guess is, and
a guess here is indistinguishable from a fact somebody published."""


def _articles_read(result: Any) -> dict[str, str]:
    """url -> text for every article the run read, for checking its quotes."""
    out: dict[str, str] = {}
    for step in result.steps:
        if step.name == "read_article" and not step.failed:
            url = str(step.arguments.get("url") or "")
            if url:
                out[url] = step.result
    return out


def _coerce(field_name: str, raw: Any) -> Any:
    """The model's value in the column's own type, or None if it cannot be.

    Refusing is the right answer for a malformed value: it arrives as a citation
    and a citation carrying `"about 250"` in a float column would fail at the
    write or, worse, coerce to something nobody said.
    """
    if raw is None:
        return None
    if field_name in {"mw_planned", "mw_built"}:
        try:
            value = float(str(raw).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    if field_name == "investment_usd":
        try:
            value = int(float(str(raw).replace(",", "").replace("$", "").strip()))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    if field_name in {"first_announced", "expected_online"}:
        try:
            return dt.date.fromisoformat(str(raw).strip()[:10])
        except ValueError:
            return None
    text = str(raw).strip()
    return text or None


def apply_facts(
    session: Any,
    project: Any,
    answer: dict[str, Any],
    *,
    articles: dict[str, str],
    gaps: set[str],
) -> tuple[list[str], list[str]]:
    """Attach the quoted facts as citations and re-derive. Returns (stored, refused).

    One `SourceRecord` per URL, so an article stating three fields becomes one
    citation asserting three claims — which is what the merge and the confidence
    scorer expect, and what a per-fact citation would have quietly inflated:
    independence is counted by domain, and three rows for one article would look
    like three sources agreeing.
    """
    from tracker.agent import verbatim
    from tracker.ingest.crawl import classify_source_type
    from tracker.ingest.records import IngestRecord, SourceRecord
    from tracker.models import utcnow
    from tracker.upsert import upsert_record

    stored: list[str] = []
    refused: list[str] = []
    by_url: dict[str, dict[str, tuple[Any, str]]] = {}

    for raw in answer.get("facts") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("field") or "").strip()
        url = str(raw.get("url") or "").strip()
        quote = str(raw.get("quote") or "").strip()
        label = f"{name} from {url or '(no url)'}"

        if name not in FILLABLE_FIELDS:
            refused.append(f"{label}: not a field this path may fill")
            continue
        if name not in gaps:
            # Not a gap any more — another fact in this same run may have filled
            # it, or a concurrent write did. Silently overwriting is how a row
            # acquires a value nobody asked for.
            refused.append(f"{label}: already known, so not a gap")
            continue
        value = _coerce(name, raw.get("value"))
        if value is None:
            refused.append(f"{label}: {raw.get('value')!r} is not a usable {name}")
            continue
        if len(quote) < MIN_QUOTE_CHARS:
            refused.append(f"{label}: quote is {len(quote)} chars, under {MIN_QUOTE_CHARS}")
            continue

        article = articles.get(url)
        if article is None:
            refused.append(f"{label}: that url was never read in this run")
            continue
        accepted, why = verbatim(quote, article)
        if not accepted:
            refused.append(f"{label}: {why}")
            continue

        by_url.setdefault(url, {})[name] = (value, accepted)

    for url, facts in by_url.items():
        record = IngestRecord(
            project={
                "company": project.company,
                "state": project.state,
                "city": project.city,
                "county": project.county,
                "name": project.name,
                **{name: value for name, (value, _quote) in facts.items()},
            },
            sources=[
                SourceRecord(
                    url=url,
                    source_type=classify_source_type(url),
                    fetched_at=utcnow(),
                    excerpt=next(iter(facts.values()))[1][:500],
                    claims={name: value for name, (value, _q) in facts.items()},
                    quotes={name: quote for name, (_v, quote) in facts.items()},
                    extractor="gapfill-agent-v1",
                )
            ],
            notes=[f"agent found {', '.join(sorted(facts))} at {url}"],
        )
        # `existing_only` and `route_to` together: this may only ever add citations
        # to the row it was asked about, never create one.
        result = upsert_record(session, record, existing_only=True, route_to=project.id)
        # `action`, not `status` — a refusal comes back as `UpsertResult(project_id=0,
        # action="refused")`, and checking the wrong attribute would report every
        # refusal as a success.
        if result.action == "refused" or result.project_id != project.id:
            refused.append(f"{url}: upsert refused it ({result.action})")
            continue
        for name in sorted(facts):
            stored.append(f"{name} = {facts[name][0]} ({url})")
            gaps.discard(name)

    return stored, refused


def fill(
    session: Any,
    project: Any,
    *,
    extractor: Any,
    gaps: list[str] | None = None,
    allow_search: bool = True,
    on_step: Any = None,
) -> Filled:
    """Let a model find and cite what this row is missing.

    `gaps` defaults to every `FILLABLE_FIELDS` column that is empty on the row.
    Passing it narrows the run — `enrich` knows which fields it cares about.
    """
    from tracker import agent

    # `gaps is not None`, not `if gaps`: an empty list is a caller saying "this row
    # needs nothing", and falling through to auto-detection there spends a call it
    # was explicitly told not to.
    if gaps is not None:
        wanted = set(gaps)
    else:
        wanted = {f for f in FILLABLE_FIELDS if getattr(project, f, None) is None}
    wanted &= FILLABLE_FIELDS
    if not wanted:
        return Filled(verdict="nothing", note="no fillable field is empty on this row")

    tools = agent.evidence_toolkit(session, allow_search=allow_search)
    tools += [record_facts_tool(sorted(wanted)), nothing_found_tool()]

    where = ", ".join(p for p in (project.city, project.county, project.state) if p)
    task = (
        f"Project #{project.id}: {project.name} — {project.company}\n"
        f"Location: {where}\n"
        f"Phase: {project.phase}\n\n"
        f"These fields are empty and need a cited source: {', '.join(sorted(wanted))}.\n"
        "Find what you can. Reporting nothing for a field nobody has published is "
        "correct; reporting a figure you cannot quote is not."
    )

    result = agent.run(task, tools=tools, extractor=extractor, system=SYSTEM, on_step=on_step)
    out = Filled(
        verdict="error",
        note=result.note,
        steps=result.tool_names,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cache_hit_tokens=result.cache_hit_tokens,
        cache_miss_tokens=result.cache_miss_tokens,
    )
    if not result.answered:
        out.verdict = "nothing" if result.outcome in {"stopped", "exhausted"} else "error"
        return out

    answer = result.answer or {}
    if result.tool_name == "nothing_found":
        out.verdict = "nothing"
        out.note = str(answer.get("reason") or "").strip()
        return out

    stored, refused = apply_facts(
        session, project, answer, articles=_articles_read(result), gaps=wanted
    )
    out.stored, out.refused = stored, refused
    out.note = str(answer.get("reason") or "").strip()
    out.verdict = "filled" if stored else "unusable"
    if not stored and not refused:
        out.verdict, out.note = "nothing", out.note or "reported no facts"
    return out


__all__ = [
    "FILLABLE_FIELDS",
    "MIN_QUOTE_CHARS",
    "SYSTEM",
    "Filled",
    "apply_facts",
    "fill",
    "nothing_found_tool",
    "record_facts_tool",
]
