"""A tool-using agent loop, and the evidence tools every caller of it needs.

**Why this exists as its own module.** Four commands already ask a model to check
something against the sources — `logic resolve`, `duplicates resolve`, `audit
resolve`, `risks confirm` — and each was written as a single `complete()` call
with a hand-built context block and a hand-written menu of answers. That shape has
two costs, both measured on the live database:

* **The menu is the ceiling.** `logic.decide` returns "nothing to choose between"
  before it ever calls the model whenever `ACTIONS[code]` is empty, and it is empty
  for 11 of the 16 rule codes — **432 of 526 findings**, of which 334 are block
  problems. The model was never the bottleneck; the list of things it was allowed
  to say was.
* **The context is a fragment.** `_triage_context` shows a 280-character excerpt
  per field. The article behind it is usually on disk, or one HTTP request away,
  and nothing was asking for it.

So the answer is not a better prompt in four places. It is one loop that hands a
model the tools to go and look — read the article, search the web, pull up a
neighbouring row — and lets it say what it concluded rather than picking a letter.

**What this module does not decide.** It runs the loop and returns what the model
asked for. Whether an answer is *believed* is the caller's business, because the
bar differs: a duplicate verdict needs a confidence and a distance check, a field
edit needs a quote that survives :func:`verbatim`. Putting that here would make one
policy serve four questions, which is how `check_collisions` and `_resolve` ended
up disagreeing about `phase` and reporting 48 repairs that never landed.

**Terminal tools are how a run ends.** A tool marked terminal is not executed —
its arguments *are* the answer, and the loop stops. That keeps "what the model
decided" in the same shape as "what the model asked for", so a caller parses one
thing and the model has one way to speak.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Steps before a run is abandoned. Generous: a thorough answer on a thin row is
#: `list_sources`, three `read_article`s, a `search_web`, one more read, then the
#: verdict — seven. The cap is a runaway guard, not a budget.
MAX_STEPS: int = 12

#: Tokens per turn. Well above `logic.MAX_TOKENS` (8000), which was measured
#: truncating replies mid-reasoning — one unusable answer in a 63-finding run, and
#: a duplicates verdict lost outright to "the reply was cut off while reasoning".
#: An agent turn also carries the whole tool transcript, so it needs more than a
#: one-shot call ever did.
MAX_TOKENS: int = 20_000

#: The retry budget when a turn comes back `finish_reason == "length"`. Truncation
#: is the one failure worth paying twice for: the model had the answer and ran out
#: of room saying it, and a second try at the same budget would stop in the same
#: place. Applied per turn and reset after, so one long deliberation does not
#: raise the price of every step behind it.
MAX_TOKENS_ESCALATED: int = 50_000

#: Characters of any single tool result handed back to the model. An uncapped
#: article is ~9,300 characters median and 24,000 at the top of the range, and a
#: run that reads six of them would spend its whole context on one row.
MAX_RESULT_CHARS: int = 8000

#: **The history is APPEND-ONLY, and that is a cost decision, not laziness.**
#:
#: The obvious optimisation is to shorten old tool results, since the whole
#: conversation is re-sent every turn and an article read on turn 3 goes over the
#: wire again on turns 4 through 10. That optimisation is WRONG on this provider.
#: DeepSeek caches on the message *prefix*: turn N's request is turn N-1's plus an
#: append, an exact prefix match, so everything re-sent bills at the cache-hit
#: rate. Editing an old message changes the prefix and makes every later turn a
#: full-price miss — the trim would raise the bill it was written to lower. This
#: was implemented, measured against the docs, and reverted.
#:
#: What follows, and must not be broken:
#:   * never mutate `messages`, only append;
#:   * keep the system prompt and tool schemas byte-identical across findings, so
#:     even the first turn of the next finding hits on that prefix;
#:   * put variable content — the project, the question — LAST.
#: `AgentResult.cache_hit_tokens` is what proves it is working.


@dataclass(frozen=True)
class Tool:
    """One capability offered to the model.

    `parameters` is a JSON Schema object, passed to the provider verbatim. `run`
    receives the parsed arguments as keywords and returns the text the model will
    read; raising is fine and is reported to the model rather than ending the run.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., str] | None = None
    #: A terminal tool is never executed. Its arguments are the answer and the
    #: loop stops, so `run` is left None for them.
    terminal: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Step:
    """One tool call and what came back, for the transcript."""

    name: str
    arguments: dict[str, Any]
    result: str = ""
    failed: bool = False


@dataclass
class AgentResult:
    """What a run concluded, and the trail it left getting there."""

    #: "answered" — a terminal tool was called; its arguments are `answer`.
    #: "exhausted" — MAX_STEPS reached with no terminal call.
    #: "stopped" — the model replied with prose and asked for nothing.
    #: "error" — the provider failed in a way retrying did not fix.
    outcome: str
    answer: dict[str, Any] | None = None
    tool_name: str | None = None
    note: str = ""
    steps: list[Step] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Turns that were retried at the bigger budget after being cut off.
    escalations: int = 0
    #: Prompt tokens the provider served from its prefix cache, and those it did
    #: not. A healthy agent run is mostly hits after turn one — the loop appends
    #: and never edits precisely so that holds. A low rate here means either the
    #: prefix is being disturbed or `cache_counts` does not know the provider's
    #: field names; `--verbose` shows the raw usage object either way.
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def answered(self) -> bool:
        return self.outcome == "answered"

    @property
    def cache_rate(self) -> float | None:
        """Share of prompt tokens served from cache, or None if unreported."""
        total = self.cache_hit_tokens + self.cache_miss_tokens
        return self.cache_hit_tokens / total if total else None

    @property
    def tool_names(self) -> list[str]:
        """What it actually looked at, in order — the useful half of a transcript."""
        return [s.name for s in self.steps]


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated at {limit} characters of {len(text)}]"


def run(
    task: str,
    *,
    tools: list[Tool],
    extractor: Any,
    system: str,
    max_steps: int = MAX_STEPS,
    max_tokens: int = MAX_TOKENS,
    escalated: int = MAX_TOKENS_ESCALATED,
    on_step: Callable[[Step], None] | None = None,
) -> AgentResult:
    """Let a model work `task` with `tools` until it calls a terminal one.

    `extractor` must satisfy `llm.ToolExtractor`. A provider without `converse`
    fails here with a clear message rather than silently answering without tools,
    which would look like a cautious model and be a dropped request.

    Every failure a tool can produce becomes a tool *result* the model reads — bad
    arguments, an unknown name, an exception. A model that asks for a page that
    404s should try another, not have the run torn down around it.
    """
    from tracker.llm import LLMError

    if not hasattr(extractor, "converse"):
        return AgentResult(
            outcome="error",
            note=(
                f"{type(extractor).__name__} cannot use tools: it has no `converse`. "
                "Use --llm-provider deepseek, or the non-agent path."
            ),
        )

    by_name = {t.name: t for t in tools}
    schemas = [t.schema() for t in tools]
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    result = AgentResult(outcome="exhausted")

    for _ in range(max_steps):
        try:
            reply = extractor.converse(
                system=system, messages=messages, tools=schemas, max_tokens=max_tokens
            )
            result.prompt_tokens += reply.prompt_tokens or 0
            result.completion_tokens += reply.completion_tokens or 0
            result.cache_hit_tokens += reply.cache_hit_tokens or 0
            result.cache_miss_tokens += reply.cache_miss_tokens or 0
            if reply.finish_reason == "length" and not reply.tool_calls:
                # It had the answer and ran out of room saying it. Retrying at the
                # same budget stops in the same place, so buy the bigger one once.
                # Only when there are no tool calls: a truncated reply that still
                # produced a complete call is usable as it stands.
                log.debug("turn truncated at %s tokens; retrying at %s", max_tokens, escalated)
                result.escalations += 1
                reply = extractor.converse(
                    system=system, messages=messages, tools=schemas, max_tokens=escalated
                )
                result.prompt_tokens += reply.prompt_tokens or 0
                result.completion_tokens += reply.completion_tokens or 0
        except LLMError as exc:
            result.outcome, result.note = "error", str(exc)
            return result

        if not reply.tool_calls:
            # Prose and no request. Usually the model explaining why it cannot
            # answer, which is worth keeping verbatim — it is the most useful
            # thing a run that decided nothing produces.
            result.outcome, result.note = "stopped", (reply.text or "").strip()
            return result

        # Echo the assistant turn back verbatim next time, or the provider rejects
        # the `tool` results as answering a request it never saw.
        messages.append(
            {
                "role": "assistant",
                "content": reply.text or "",
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.raw_arguments or "{}"},
                    }
                    for c in reply.tool_calls
                ],
            }
        )

        for call in reply.tool_calls:
            tool = by_name.get(call.name)

            if tool is not None and tool.terminal:
                step = Step(name=call.name, arguments=call.arguments)
                result.steps.append(step)
                if on_step:
                    on_step(step)
                result.outcome = "answered"
                result.answer = call.arguments
                result.tool_name = call.name
                return result

            if tool is None:
                payload, failed = (
                    f"no such tool: {call.name}. Available: {', '.join(sorted(by_name))}",
                    True,
                )
            elif call.parse_failed:
                # `parse_failed`, never `not call.arguments`: an empty dict is
                # also what a correct call to a no-argument tool looks like.
                payload, failed = (
                    f"your arguments were not a JSON object: {call.raw_arguments[:200]}",
                    True,
                )
            else:
                try:
                    payload, failed = _clip(tool.run(**call.arguments) if tool.run else ""), False
                except TypeError as exc:
                    payload, failed = f"wrong arguments for {call.name}: {exc}", True
                except Exception as exc:
                    log.debug("tool %s failed", call.name, exc_info=True)
                    payload, failed = f"{call.name} failed: {exc}", True

            step = Step(name=call.name, arguments=call.arguments, result=payload, failed=failed)
            result.steps.append(step)
            if on_step:
                on_step(step)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": payload})

    result.note = f"reached {max_steps} steps without deciding"
    return result


# --- verification -----------------------------------------------------------


def verbatim(quote: str, article: str) -> tuple[str, str]:
    """Return `(the sentence as the article words it, refusal)`. One is empty.

    The field-agnostic half of `riskcheck.verify_quote`, which also asks whether
    the sentence states an obstacle *of a named category* — right for a risk and
    meaningless for `mw_planned`. Both call the same `_verbatim_run`, so a quote
    accepted here is accepted by the extraction path's gate on the same terms.

    Why any of this. The evidence tier is computed from whether a quote backs the
    value: an edit with one stores as `reported`, an edit without stores as
    `inferred`, and **`capex` does not sum `inferred`**. An agent writing freely
    without quotes would do correct work that never reaches a published total.

    A quote must match at least `crawl.MIN_RUN_CHARS` (40) characters and half its
    own length. Callers prompting an agent should ask for a whole sentence: "250
    MW" is in every article about a 250 MW site and evidences nothing, which is
    what the floor is there to refuse.
    """
    from tracker.ingest.crawl import _verbatim_run

    if not (quote or "").strip():
        return "", "no quote offered"
    if not (article or "").strip():
        return "", "no article text to check the quote against"
    run_ = _verbatim_run(quote, article)
    if not run_.text:
        return "", "that sentence is not in the source text"
    return run_.text, ""


# --- the shared toolkit -----------------------------------------------------


def evidence_toolkit(
    session: Any, *, cache_dir: Any = None, allow_search: bool = True
) -> list[Tool]:
    """The tools any caller checking a claim against sources wants.

    Bound to a session so they read the live database, and returned as a plain
    list so a caller can drop one it does not want and append its own terminal
    tool. Nothing here writes.
    """
    tools = [
        Tool(
            name="list_sources",
            description=(
                "The citations attached to a project: id, publisher, date, url, and the "
                "stored excerpt. Start here — it tells you which articles are worth reading."
            ),
            parameters={
                "type": "object",
                "properties": {"project_id": {"type": "integer"}},
                "required": ["project_id"],
            },
            run=lambda project_id: _list_sources(session, int(project_id)),
        ),
        Tool(
            name="read_article",
            description=(
                "The full text behind a url. Served from the local article cache, and "
                "fetched and cached on a miss. Use it when an excerpt is too short to "
                "settle the question."
            ),
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            run=lambda url: _read_article(str(url), cache_dir=cache_dir),
        ),
        Tool(
            name="show_project",
            description=(
                "One project row in full: every tracked field, its tranches, milestones, "
                "open obstacles and notes."
            ),
            parameters={
                "type": "object",
                "properties": {"project_id": {"type": "integer"}},
                "required": ["project_id"],
            },
            run=lambda project_id: _show_project(session, int(project_id)),
        ),
        Tool(
            name="find_projects",
            description=(
                "Search stored projects by name, company, city, county or state. Use it to "
                "check whether a site is already held under another name."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "description": "default 10"},
                },
                "required": ["query"],
            },
            run=lambda query, limit=10: _find_projects(session, str(query), int(limit)),
        ),
    ]
    if allow_search:
        tools.append(
            Tool(
                name="search_web",
                description=(
                    "Search the web for pages this database has never read. Use it only "
                    "when the stored sources cannot settle the question; then read_article "
                    "a result to quote from it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "description": "default 5"},
                    },
                    "required": ["query"],
                },
                run=lambda query, limit=5: _search_web(str(query), int(limit)),
            )
        )
    return tools


def _list_sources(session: Any, project_id: int) -> str:
    from tracker.models import Project

    project = session.get(Project, project_id)
    if project is None:
        return f"no project #{project_id}"
    lines = [f"{len(project.sources)} citation(s) on #{project_id}:"]
    for src in project.sources:
        excerpt = (src.excerpt or "").strip().replace("\n", " ")
        lines.append(
            f"- [{src.id}] {src.source_type or 'unknown'} "
            f"{src.published_at or 'undated'}  {src.url}"
        )
        if excerpt:
            lines.append(f'    "{excerpt[:300]}"')
    return "\n".join(lines)


def _read_article(url: str, *, cache_dir: Any = None) -> str:
    """Cached text for a url, fetching and caching on a miss.

    Lazy rather than pre-warmed. The cache holds 498 of 2,103 cited urls, and a
    pass to fill the rest would be ~1,600 speculative requests at hosts that have
    already answered this database with 634 403s. Fetching only what a run asks
    for spends a request per article actually wanted, and every one makes the next
    run cheaper. Measured success on a 150-url tranche: 145.
    """
    import asyncio
    from pathlib import Path

    from tracker.config import cache_dir as config_cache_dir
    from tracker.config import get_settings
    from tracker.ingest.fetch import HttpxFetcher, cache_path, fetch_with_retry

    root = Path(cache_dir) if cache_dir else config_cache_dir("articles")
    path = cache_path(url, root)
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            pass

    async def go() -> Any:
        fetcher = HttpxFetcher(get_settings())
        return await fetch_with_retry(fetcher, url)

    try:
        result = asyncio.run(go())
    except Exception as exc:
        return f"could not fetch {url}: {exc}"
    if not result.ok or not (result.markdown or "").strip():
        return f"could not fetch {url}: {result.error or 'no text'}"

    try:
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(result.markdown, encoding="utf-8")
    except OSError:
        pass  # readable now is what matters; the cache is an optimisation
    return result.markdown


def _show_project(session: Any, project_id: int) -> str:
    from tracker.models import Project

    project = session.get(Project, project_id)
    if project is None:
        return f"no project #{project_id}"
    show = lambda v: "unknown" if v is None else str(v)  # noqa: E731
    lines = [
        f"#{project.id} {project.name} — {project.company}",
        f"  location: {project.city or project.county or '?'}, {project.state}"
        f"  county={show(project.county)}  coords={show(project.lat)},{show(project.lon)}",
        f"  phase: {show(project.phase)}   confidence: {show(project.confidence)}",
        f"  mw_planned: {show(project.mw_planned)}   mw_built: {show(project.mw_built)}",
        f"  investment_usd: {show(project.investment_usd)}",
        f"  first_announced: {show(project.first_announced)}"
        f"   expected_online: {show(project.expected_online)}",
        f"  customer: {show(project.customer)}   blocker: {show(project.blocker)}",
    ]
    blocks = list(getattr(project, "blocks", ()) or ())
    if blocks:
        lines.append(f"  tranches ({len(blocks)}):")
        for b in blocks[:25]:
            # `unconfirmed_fields` is the comma-separated list the 待确认 tier is
            # recorded in; there is no `mw_confirmed` attribute to ask.
            unconfirmed = {f.strip() for f in (b.unconfirmed_fields or "").split(",") if f.strip()}
            lines.append(
                f"    {b.label} — {show(b.mw)} MW  {b.status}"
                f"{'  [mw 待确认 — no quote states it]' if 'mw' in unconfirmed else ''}"
            )
    events = sorted(
        {(e.event_type, str(e.event_date)) for e in getattr(project, "events", ()) or ()}
    )
    if events:
        lines.append("  milestones: " + ", ".join(f"{t}={d}" for t, d in events))
    if project.notes:
        lines.append(f"  notes: {project.notes[:1200]}")
    return "\n".join(lines)


def _find_projects(session: Any, query: str, limit: int = 10) -> str:
    from sqlalchemy import or_, select

    from tracker.models import Project

    like = f"%{query.strip()}%"
    rows = session.scalars(
        select(Project)
        .where(
            or_(
                Project.name.ilike(like),
                Project.company.ilike(like),
                Project.city.ilike(like),
                Project.county.ilike(like),
                Project.state.ilike(like),
            )
        )
        .limit(max(1, min(limit, 25)))
    ).all()
    if not rows:
        return f"no project matches {query!r}"
    return "\n".join(
        f"#{p.id} {p.name} — {p.company} — "
        f"{p.city or p.county or '?'}, {p.state} — {p.phase} — "
        f"{p.mw_planned if p.mw_planned is not None else '?'} MW planned"
        for p in rows
    )


def _search_web(query: str, limit: int = 5) -> str:
    from tracker.ingest.search import SearchError, build_provider

    try:
        provider = build_provider()
    except Exception as exc:
        return f"web search unavailable: {exc}"
    if provider is None:
        return "web search unavailable: no search provider is configured"
    try:
        hits = provider.search(query, limit=max(1, min(limit, 10)))
    except SearchError as exc:
        return f"search failed: {exc}"
    except Exception as exc:
        return f"search failed: {exc}"
    hits = list(hits or ())
    if not hits:
        return f"no results for {query!r}"
    return "\n".join(
        f"- {getattr(h, 'title', '') or '(untitled)'}\n  {getattr(h, 'url', '')}\n"
        f"  {(getattr(h, 'snippet', '') or '').strip()[:300]}"
        for h in hits
    )


__all__ = [
    "MAX_RESULT_CHARS",
    "MAX_STEPS",
    "MAX_TOKENS",
    "MAX_TOKENS_ESCALATED",
    "AgentResult",
    "Step",
    "Tool",
    "evidence_toolkit",
    "run",
    "verbatim",
]
