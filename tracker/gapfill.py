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
    #: Fields this run went after and did not fill. `apply_facts` already computes
    #: it exactly — it discards each field it stores from the `gaps` set handed to
    #: it — and until now the answer was thrown away. `tracker.attempts` records it
    #: so the next run does not pay to ask the same unanswerable question again.
    missed: tuple[str, ...] = ()
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


def _fact_axes(
    facts: dict[str, tuple[Any, str]], site_names: frozenset[str] = frozenset()
) -> dict[str, dict[str, Any]]:
    """The claim envelope for the facts one citation carries: `basis`, `scope` and
    `date_precision`.

    A thin adapter onto the same gate the crawl path uses, rather than a second
    reading of the same wording: `axis_gate` owns which phrases license which
    basis, and which make a figure one building's rather than the campus's, and two
    copies of those tables would be free to disagree.

    Only non-default answers are stored, matching `crawl._claim_axes`. Writing
    `unspecified` or `unnamed` would attach an envelope to nearly every claim and
    inflate the coverage measurement that decides whether these axes are worth
    keeping.
    """
    from tracker.ingest.crawl import axis_gate
    from tracker.normalize import precision_in_quote
    from tracker.vocab import BASIS_FIELDS, CLAIM_AXIS_DEFAULTS, PART_FIELDS

    out: dict[str, dict[str, Any]] = {}
    for name, (value, quote) in facts.items():
        if name in ("first_announced", "expected_online"):
            # The crawl path's reading of how precisely the sentence gave the date.
            # Without it "in 2027" stored as 2027-01-01 counted as a promised day,
            # and the overdue check fired the morning the year began.
            stated = precision_in_quote(value, quote)
            if stated:
                out[name] = {"date_precision": stated}
            continue
        if name not in BASIS_FIELDS and name not in PART_FIELDS:
            continue
        axes = axis_gate({}, quote, site_names=site_names, field=name, value=value)
        kept = {
            axis: axes[axis]
            for axis in ("basis", "scope")
            if axis in axes and axes[axis] != CLAIM_AXIS_DEFAULTS[axis]
        }
        if kept:
            out[name] = kept
    return out


def _names_of(project: Any) -> frozenset[str]:
    """What a quote can call this row, for telling one of its buildings from it."""
    from tracker.ingest.crawl import site_names

    return site_names({"name": project.name, "city": project.city, "county": project.county})


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
                    # Which kind of megawatt, and whether the figure is one
                    # building's, on the same terms as the crawl path.
                    #
                    # This path builds its own citation rather than going through
                    # the article reader, so without this the agent could fill a
                    # capacity that carried no basis while `sync` filling the same
                    # column carried one — the same field behaving differently
                    # depending on which command wrote it. It costs nothing: the
                    # quote is already verified verbatim above and the figure is
                    # in hand, which is everything the axis reads.
                    claim_meta=_fact_axes(facts, _names_of(project)),
                    extractor="gapfill-agent-v1",
                )
            ],
            notes=[f"agent found {', '.join(sorted(facts))} at {url}"],
        )
        # `existing_only` and `route_to` together: this may only ever add citations
        # to the row it was asked about, never create one. `reading` because the
        # agent reports only the gaps it came for: on a URL the row already cites,
        # replacing the citation, as a re-read of the whole article does, would
        # erase every other claim it made. Folded, the first reading stands.
        result = upsert_record(
            session,
            record,
            existing_only=True,
            route_to=project.id,
            reading={(project.id, s.url) for s in project.sources},
        )
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
    `question`, `investigate` and `conclude` in a row, which is what `fill_rows`
    runs for many rows with the runs overlapping.
    """
    asked = question(project, gaps)
    if isinstance(asked, Filled):
        return asked
    result = investigate(
        asked, extractor=extractor, db=session, allow_search=allow_search, on_step=on_step
    )
    return conclude(session, project, asked, result)


@dataclass(frozen=True)
class Question:
    """One row's question as plain values: which fields, and the agent's task."""

    project_id: int
    wanted: tuple[str, ...]
    task: str


def question(project: Any, gaps: list[str] | None = None) -> Question | Filled:
    """The question for one row, or the `Filled` that needs no question."""
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

    where = ", ".join(p for p in (project.city, project.county, project.state) if p)
    task = (
        f"Project #{project.id}: {project.name} — {project.company}\n"
        f"Location: {where}\n"
        f"Phase: {project.phase}\n\n"
        f"These fields are empty and need a cited source: {', '.join(sorted(wanted))}.\n"
        "Find what you can. Reporting nothing for a field nobody has published is "
        "correct; reporting a figure you cannot quote is not."
    )
    return Question(project.id, tuple(sorted(wanted)), task)


def investigate(
    asked: Question, *, extractor: Any, db: Any, allow_search: bool = True, on_step: Any = None
) -> Any:
    """The agent run for one row. Writes nothing; `db` is what its evidence tools read."""
    from tracker import agent

    tools = agent.evidence_toolkit(db, allow_search=allow_search)
    tools += [record_facts_tool(list(asked.wanted)), nothing_found_tool()]
    return agent.run(asked.task, tools=tools, extractor=extractor, system=SYSTEM, on_step=on_step)


def conclude(session: Any, project: Any, asked: Question, result: Any) -> Filled:
    """Store what the run found, under the evidence gate. Main thread."""
    from tracker import agent

    wanted = set(asked.wanted)
    out = Filled(
        verdict="error",
        note=result.note,
        steps=result.tool_names,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cache_hit_tokens=result.cache_hit_tokens,
        cache_miss_tokens=result.cache_miss_tokens,
    )
    # A run whose searches the night's cap refused never looked: its "nothing" is
    # an error, and records no attempt (`tracker enrich` rolls an error back).
    refused_search = any(
        step.name == "search_web" and str(step.result).startswith(agent.SEARCH_REFUSED)
        for step in result.steps
    )
    if refused_search and result.tool_name != "record_facts":
        out.verdict, out.missed = "error", ()
        out.note = "the night's search cap refused its searches; nothing was looked for"
        return out
    if not result.answered:
        out.verdict = "nothing" if result.outcome in {"stopped", "exhausted"} else "error"
        # An error is not evidence that nobody published the field, so only a run
        # that actually reached an answer reports what it missed. Recording a
        # provider timeout as "looked and found nothing" would retire a field on
        # the strength of a network failure.
        out.missed = tuple(sorted(wanted)) if out.verdict == "nothing" else ()
        return out

    answer = result.answer or {}
    if result.tool_name == "nothing_found":
        out.verdict = "nothing"
        out.note = str(answer.get("reason") or "").strip()
        out.missed = tuple(sorted(wanted))
        return out

    stored, refused = apply_facts(
        session, project, answer, articles=_articles_read(result), gaps=wanted
    )
    # `apply_facts` discarded every field it stored, so `wanted` is now exactly what
    # was asked for and not found.
    out.missed = tuple(sorted(wanted))
    out.stored, out.refused = stored, refused
    out.note = str(answer.get("reason") or "").strip()
    out.verdict = "filled" if stored else "unusable"
    if not stored and not refused:
        out.verdict, out.note = "nothing", out.note or "reported no facts"
    return out


#: What one row of the agent pass typically costs. Used only to decide whether the
#: NEXT row fits in what is left of a token budget, before any run has measured a
#: real figure. See `README.md` for where the number comes from.
TYPICAL_ROW_TOKENS = 77_000


@dataclass
class Row:
    """One row of the agent pass, for the report."""

    project_id: int
    name: str
    #: The fields it was asked about; empty when it was asked nothing.
    asked: tuple[str, ...] = ()
    filled: Filled | None = None
    #: Fields filled on the row now — what reached it, not what was "stored".
    landed: tuple[str, ...] = ()
    #: Fields found again but struck by an earlier ruling, so not stored.
    unlanded: tuple[str, ...] = ()
    error: str = ""


def fill_rows(
    session: Any,
    project_ids: list[int],
    *,
    extractor: Any,
    only_fields: tuple[str, ...] | None = None,
    token_budget: int = 0,
    max_attempts: int = 2,
    t2_only: bool = False,
    workers: int = 1,
    on_each: Any = None,
) -> Any:
    """The agent pass over many rows, through `judged.run`. Returns its `Run` of `Row`.

    Which fields each row is asked about: empty, wanted (`only_fields`, and with
    `t2_only` only the gaps that hold it below T2), and not already asked about to
    exhaustion (`tracker.attempts`). A row with nothing left to ask costs nothing.

    Each row is committed as it is concluded — the facts it stored, and the attempt
    note for the fields it looked for and did not find, which is the whole saving
    and must not be rolled back. A run that errored or answered unusably is rolled
    back: it says something about the call, not about whether anybody published the
    figure. It is not held back for the night the way the other passes' failures
    are — `model_decline` has no kind for this pass, and adding one is a migration —
    so the next round asks it again.

    `token_budget` stops the pass between rows, never mid-row: aborting a run in
    flight would spend the tokens and store nothing. With several workers a wave
    starts only as many rows as the budget still has room for, at what a row has
    cost so far.
    """
    from tracker import attempts as attempts_mod
    from tracker import judged
    from tracker.models import Project

    spent = {"tokens": 0, "called": 0}

    def askable(project: Any) -> list[str]:
        empty = {f for f in FILLABLE_FIELDS if getattr(project, f, None) is None}
        if only_fields is not None:
            empty &= set(only_fields)
        if t2_only:
            from tracker.ingest.enrich import t2_gaps

            empty &= t2_gaps(project)
        return sorted(empty - attempts_mod.exhausted(project, max_attempts=max_attempts))

    def prepare(project_id: int) -> Any:
        project = session.get(Project, project_id)
        if project is None:
            return judged.Done(result=None)
        wanted = askable(project)
        if not wanted:
            return judged.Done(Row(project.id, project.name))
        asked = question(project, wanted)
        return asked if isinstance(asked, Question) else judged.Done(Row(project.id, project.name))

    def ask(asked: Question, db: Any) -> Any:
        return investigate(asked, extractor=extractor, db=db)

    def settle(project_id: int, asked: Question, result: Any) -> Any:
        project = session.get(Project, project_id)
        out = conclude(session, project, asked, result)
        spent["tokens"] += out.prompt_tokens + out.completion_tokens
        spent["called"] += 1
        # A fact can be "stored" and still not reach the row: attached to a citation
        # whose claim about that field an earlier ruling struck, it stays struck.
        # COL4 re-found the same ruled-out $150M on two nights running and reported
        # it as gained both times. What counts is whether the field is filled now.
        landed = tuple(f for f in asked.wanted if getattr(project, f, None) is not None)
        unlanded = tuple(f for f in asked.wanted if f not in landed and f not in out.missed)
        row = Row(project.id, project.name, asked.wanted, out, landed, unlanded)
        # Only a run that reached an answer is evidence about the field. A fact that
        # did not land is an answer that changed nothing, and is recorded the same
        # way, so the next night does not pay to re-find it.
        if out.verdict in {"filled", "nothing"}:
            if out.missed or unlanded:
                attempts_mod.record(project, [*out.missed, *unlanded])
        else:
            session.rollback()
        return judged.Done(row)

    def failed(project_id: int, error: BaseException) -> Row:
        project = session.get(Project, project_id)
        return Row(project_id, project.name if project is not None else "", error=str(error))

    def room() -> int | None:
        if not token_budget:
            return None
        # `expected` is what a row has actually cost this run; the constant only
        # stands in until a row has been measured.
        expected = spent["tokens"] // spent["called"] if spent["called"] else TYPICAL_ROW_TOKENS
        return max(0, (token_budget - spent["tokens"]) // max(1, expected))

    return judged.run(
        session,
        project_ids,
        kind=None,
        subject=str,
        fingerprint=str,
        projects=lambda project_id: [project_id],
        prepare=prepare,
        ask=ask,
        apply=settle,
        failed=failed,
        workers=workers,
        reads_db=True,
        stage="agent",
        room=room,
        on_each=on_each,
        label="gapfill",
    )


__all__ = [
    "FILLABLE_FIELDS",
    "MIN_QUOTE_CHARS",
    "SYSTEM",
    "TYPICAL_ROW_TOKENS",
    "Filled",
    "Question",
    "Row",
    "apply_facts",
    "conclude",
    "fill",
    "fill_rows",
    "investigate",
    "nothing_found_tool",
    "question",
    "record_facts_tool",
]
