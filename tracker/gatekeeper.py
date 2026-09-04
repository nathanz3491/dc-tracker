"""Deciding, at write time, whether an arriving article is a site we already hold.

**Prevention rather than cleanup, and the arithmetic is why.** A duplicate created
at ingest costs, in order: a row nobody wanted; one side of the pair held out of
every `capex` total until somebody settles it; and then either a person reading two
sets of citations or a merge that deletes a row and cannot be undone. Measured on
this database: 47 suspected groups holding 22,012 MW twice, and clearing most of
them took a ten-hour agent run and ¥100. The same judgement made *before* the
insert costs one call and deletes nothing.

**Why the write path could not already do this.** `upsert_record` decides
insert-versus-update from `dedup_key`, and `dedup.py`'s founding invariant is that
a county-level row and a city-level row are never merged automatically because "no
string comparison can tell whether 'Racine County' and 'Mount Pleasant' are one
project". That is correct and it is also the whole problem: the evidence that would
settle it is in the article being ingested, and nothing was reading it.
`_find_duplicate_candidate` finds the near-misses perfectly well — it just runs
after the insert, where all it can do is attach a warning.

So this is the same judgement `triage.pair_triage` makes on stored rows, moved to
the moment it is cheap.

**It fails open, always.** An arbiter that is unsure, that errors, or whose
confidence is short returns None and the row inserts exactly as it does today. The
duplicate report still runs. The worst case is the status quo, which is what makes
this safe to leave on.

**A wrong route is worse than a duplicate, so the bar is high.** Two genuinely
different sites sharing one row cannot be un-merged, whereas a duplicate can always
be folded later. Hence a 0.9 floor, a refusal when the article was never read, the
same geographic sanity check `dupresolve` applies, and the decision written into the
row's notes so it is auditable rather than invisible.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Below this a route is refused and the row inserts. Deliberately higher than the
#: 0.85 a *merge* of two stored rows needs: a merge is reviewed against two full
#: rows of citations, while this is decided from one arriving article.
MIN_CONFIDENCE: float = 0.9

#: How the cold path works: a tool loop that has to go and fetch its own evidence.
#: Split from the judgement policy below because the warm path has no tool loop —
#: it is handed the article and the candidate outright — and must not inherit
#: instructions to call tools it was never offered.
_COLD_INTRO = """You decide whether an article describes a data centre campus already in a database.

You are called at the moment a new row would be created. If the article is about a
site already held, saying so attaches its citations to that row instead — which is
right, and cheap. If it is a different site, saying so creates the row — also right.

How to work:
1. `show_project` the candidate row, and `list_sources` to see what it already cites.
2. `read_article` the arriving url. This is the evidence that decides it, and no
   string comparison has it.
3. `find_projects` if you suspect a third row is involved.
4. Then `same_site`, `different_site`, or `unsure`."""

#: The judgement itself, shared by both paths **verbatim**. One copy, because the
#: first paragraph is what guards the irreversible direction: the same-locality,
#: different-company branch of `upsert._find_duplicate_candidate` is exactly the
#: builder/landlord/occupier case, and a path that lost this warning would route
#: three legitimate rows into one.
RULES = """One real campus routinely has a builder, a landlord and an occupier, and each name
makes its own legitimate row. Two rows are the same site only when they are the same
PHYSICAL PLACE — the same address, parcel, substation or building count.

The commonest true match is GRANULARITY: one row filed under a city and the other
under the county containing it. Check that the city really is in that county rather
than assuming it from the names.

`unsure` is a good answer and costs almost nothing: the row is created and the pair
is reported for review, which is what happens today anyway. Saying `same_site`
wrongly puts two different campuses in one row and cannot be undone. When the
article does not settle it, say `unsure`."""

#: Byte-identical to what the cold path has always sent. `agent.run` re-sends the
#: system message on every turn, so a change here moves a cache prefix.
SYSTEM = f"{_COLD_INTRO}\n\n{RULES}"

#: The warm path has no tool loop and nothing to fetch: the article, the proposal
#: and the stored row are all in the one message it gets. It must therefore not
#: inherit `_COLD_INTRO`'s instructions to call tools it was never offered.
_WARM_INTRO = """You decide whether an article describes a data centre campus already in a database.

You have just read the article below and proposed a new row from it. Before that row
is created you are being shown an existing row it may duplicate, and asked to settle
it. Everything needed is in this message — the article, your proposal, and the stored
row. There are no tools for fetching more, and nothing to look up.

The database's suspicion is a suspicion, not a finding. Saying it is wrong is a
useful answer and costs nothing."""

#: Same judgement, same words, different preamble.
SYSTEM_WARM = f"{_WARM_INTRO}\n\n{RULES}"


@dataclass
class _Verdict:
    """What a judgement produced, however it was reached.

    One shape for both paths so the rails below — the confidence floor, the
    geography check, the quote gate, the audit line — are written once and cannot
    drift into two versions that disagree about what refuses a route.

    `articles` is the haystack the quote is checked against: **what the model was
    actually shown**. The cold path fills it from the `read_article` results it
    fetched; the warm path fills it with the article body it was handed. Without
    this the warm path would arrive with an empty haystack and refuse every
    verdict while reporting nothing unusual.
    """

    outcome: str
    tool_name: str | None = None
    answer: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    steps: list[str] = field(default_factory=list)
    articles: dict[str, str] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    via: str = "cold"

    @property
    def answered(self) -> bool:
        return self.outcome == "answered"


def _verdict_tools(candidate_id: int) -> list[Any]:
    from tracker.agent import Tool

    confidence = {"type": "number", "description": "0 to 1."}
    return [
        Tool(
            name="same_site",
            description=(
                f"This article describes project #{candidate_id}. Its citations will be "
                "attached to that row instead of a new one being created. No row is "
                "deleted, but two different campuses sharing one row cannot be undone."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "quote": {
                        "type": "string",
                        "description": (
                            "A sentence from the article you read that ties it to that "
                            "row's place — the address, county, parcel or buildings."
                        ),
                    },
                    "confidence": confidence,
                },
                "required": ["reason", "confidence"],
            },
            terminal=True,
        ),
        Tool(
            name="different_site",
            description="A different campus. A new row is created, as it would be anyway.",
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string"}, "confidence": confidence},
                "required": ["reason", "confidence"],
            },
            terminal=True,
        ),
        Tool(
            name="unsure",
            description=(
                "The article does not settle it. The row is created and the pair is "
                "reported for review. This is a respected answer."
            ),
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
            terminal=True,
        ),
    ]


def _incoming_urls(record: Any) -> list[str]:
    return [s.url for s in (getattr(record, "sources", None) or ()) if getattr(s, "url", None)]


#: A model's reason, made safe to store. `logic.record_decision` writes one line
#: and dedupes with `if line not in lines`, so a multi-line reason never matches
#: itself on the next read and the row's notes grow on every re-crawl.
def _one_line(text: str, limit: int = 400) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit]


def _where(payload: dict[str, Any]) -> str:
    return ", ".join(
        p for p in (payload.get("city"), payload.get("county"), payload.get("state")) if p
    )


def _cold_verdict(
    extractor: Any,
    *,
    session: Any,
    payload: dict[str, Any],
    candidate: Any,
    urls: list[str],
    allow_search: bool,
) -> _Verdict:
    """The original path: a tool loop that fetches its own evidence.

    Kept for every caller that has no extraction context — a hand-curated or ISO
    record, a provider with no `converse`, and any future writer that reaches
    `upsert_record` without going through `crawl.run`.
    """
    from tracker import agent

    task = (
        "An article would create a NEW row. Is it the same site as the candidate?\n\n"
        f"ARRIVING, from {urls[0]}\n"
        f"  company:  {payload.get('company') or 'unknown'}\n"
        f"  name:     {payload.get('name') or 'unknown'}\n"
        f"  location: {_where(payload) or 'unknown'}\n\n"
        f"CANDIDATE ALREADY STORED\n"
        f"  #{candidate.id} {candidate.name} — {candidate.company}\n"
        f"  location: "
        f"{candidate.city or candidate.county or '?'}, {candidate.state}"
        f"  (city={candidate.city}, county={candidate.county})\n\n"
        f"Read {urls[0]} before deciding."
    )

    tools = agent.evidence_toolkit(session, allow_search=allow_search)
    tools += _verdict_tools(candidate.id)
    result = agent.run(task, tools=tools, extractor=extractor, system=SYSTEM)

    return _Verdict(
        outcome=result.outcome,
        tool_name=result.tool_name,
        answer=result.answer or {},
        note=result.note,
        steps=list(result.tool_names),
        # What it actually read, at the clip length the tool served it.
        articles={
            step.arguments.get("url"): step.result
            for step in result.steps
            if step.name == "read_article" and not step.failed
        },
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cache_hit_tokens=result.cache_hit_tokens,
        cache_miss_tokens=result.cache_miss_tokens,
        via="cold",
    )


def _suspicion(key: str, candidate: Any) -> str:
    """Why the write path stopped, in the words a reader would use.

    Named rather than implied, because the three branches of
    `upsert._find_duplicate_candidate` are not equally dangerous and the model
    should know which one it is answering. The third is the builder/landlord/
    occupier case, where routing wrongly folds legitimate rows together — so it
    says so, and `RULES` opens with exactly that warning.
    """
    from tracker.dedup import is_cross_granularity_match

    theirs = getattr(candidate, "dedup_key", "") or ""
    if theirs and is_cross_granularity_match(key, theirs):
        return (
            "one of these is filed under a city and the other under a county, and the "
            "two may be the same place recorded at different precision"
        )
    if theirs and key.split("|", 1)[0] == theirs.split("|", 1)[0]:
        return "both carry the same operator and state, and their locality keys overlap"
    return (
        "both sit in the same locality under different company names — which is also "
        "what a builder, a landlord and an occupier filing separately looks like"
    )


def _rejection(
    session: Any, *, payload: dict[str, Any], candidate: Any, key: str, body: str
) -> str:
    """The one turn the warm path sends.

    The candidate is described by calling the same functions the cold path's
    `show_project` and `list_sources` tools wrap, so the two can never drift into
    showing a different picture of the same row.

    The arriving row is named by company, name and locality and **never by
    position**: `build_records` drops entries with no locality and truncates at
    `MAX_PROJECTS_PER_ARTICLE`, so its index is not the index in the model's own
    reply, and "your second project" can name the wrong campus.
    """
    from tracker.agent import _list_sources, _show_project

    where = _where(payload) or "unknown"
    return (
        "Your proposal would create a new row, and the database has stopped it.\n\n"
        "YOUR PROPOSAL\n"
        f"  company:  {payload.get('company') or 'unknown'}\n"
        f"  name:     {payload.get('name') or 'unknown'}\n"
        f"  location: {where}\n\n"
        f"WHY IT WAS STOPPED\n  {_suspicion(key, candidate)}.\n\n"
        "THE ROW IT MAY DUPLICATE\n"
        f"{_show_project(session, candidate.id)}\n"
        f"{_list_sources(session, candidate.id)}\n\n"
        "THE ARTICLE YOU READ\n"
        "(the same text you were given; you do not need to fetch it again)\n\n"
        f"{body}\n\n"
        "Is your proposal the same physical site as the stored row? Call exactly one "
        "of `same_site`, `different_site` or `unsure`. For `same_site`, quote the "
        "sentence from the article above that ties the two to one place."
    )


def _warm_verdict(
    extractor: Any,
    *,
    session: Any,
    context: Any,
    payload: dict[str, Any],
    candidate: Any,
    key: str,
) -> _Verdict:
    """One call, with the evidence already in hand.

    No tool loop: the cold path's three-to-four turns exist only to fetch the
    article and the candidate row, and both are supplied here. The verdict tools
    stay, because every rail below reads structured arguments — a confidence, a
    quote — and parsing those out of prose is what terminal tools removed.
    """
    from tracker.agent import MAX_TOKENS, MAX_TOKENS_ESCALATED
    from tracker.llm import LLMError

    body = context.body()
    schemas = [t.schema() for t in _verdict_tools(candidate.id)]
    messages = [
        {
            "role": "user",
            "content": _rejection(
                session, payload=payload, candidate=candidate, key=key, body=body
            ),
        }
    ]

    verdict = _Verdict(outcome="error", via="warm", articles={context.url: body})
    try:
        reply = extractor.converse(
            system=SYSTEM_WARM, messages=messages, tools=schemas, max_tokens=MAX_TOKENS
        )
        verdict.prompt_tokens += reply.prompt_tokens or 0
        verdict.completion_tokens += reply.completion_tokens or 0
        verdict.cache_hit_tokens += reply.cache_hit_tokens or 0
        verdict.cache_miss_tokens += reply.cache_miss_tokens or 0
        if reply.finish_reason == "length" and not reply.tool_calls:
            # Mirrors `agent.run`: a turn that ran out of room mid-reasoning is
            # retried once at the bigger budget rather than thrown away, because
            # the prompt has already been paid for.
            reply = extractor.converse(
                system=SYSTEM_WARM,
                messages=messages,
                tools=schemas,
                max_tokens=MAX_TOKENS_ESCALATED,
            )
            verdict.prompt_tokens += reply.prompt_tokens or 0
            verdict.completion_tokens += reply.completion_tokens or 0
            verdict.cache_hit_tokens += reply.cache_hit_tokens or 0
            verdict.cache_miss_tokens += reply.cache_miss_tokens or 0
    except LLMError as exc:
        # Reported, not swallowed: an outage that returned no verdict must not
        # look in the run summary like a model that was merely cautious.
        verdict.note = str(exc)[:400]
        return verdict
    except Exception as exc:  # a provider with no `converse`, or anything else
        verdict.note = f"{type(exc).__name__}: {exc}"[:400]
        return verdict

    names = {"same_site", "different_site", "unsure"}
    call = next((c for c in reply.tool_calls if c.name in names), None)
    if call is None:
        # Prose, or a tool nobody offered. A real answer about nothing.
        verdict.outcome = "stopped"
        verdict.note = (reply.text or "").strip()[:400]
        return verdict

    verdict.outcome = "answered"
    verdict.tool_name = call.name
    verdict.answer = call.arguments or {}
    verdict.steps = [call.name]
    return verdict


def same_site_arbiter(
    extractor: Any,
    *,
    min_confidence: float = MIN_CONFIDENCE,
    require_quote: bool = True,
    allow_search: bool = True,
    on_decision: Any = None,
) -> Any:
    """Build the callable `upsert_record(arbiter=...)` expects.

    A factory rather than a function so the extractor and the thresholds are bound
    once per run instead of per record — an ingest of 300 articles must not rebuild
    a provider client 300 times.
    """

    def arbitrate(
        *,
        session: Any,
        record: Any,
        payload: dict[str, Any],
        key: str,
        candidate: Any,
        context: Any = None,
    ) -> int | None:
        from tracker.agent import verbatim
        from tracker.dupresolve import FAR_APART_KM, km_apart

        urls = _incoming_urls(record)
        if not urls:
            # A record with no citation is a hand-curated or ISO row. There is no
            # article to read, so there is nothing here this cannot already do.
            return None

        # The warm path only where the context provably belongs to THIS record.
        # One arbiter closure serves a whole run, so a context left bound from an
        # earlier article would judge this record against that article's text —
        # wrong in a way nothing downstream could detect.
        warm = context is not None and getattr(context, "url", None) in urls
        if warm and not hasattr(extractor, "converse"):
            # A provider with no multi-turn call. The cold path already reports
            # this properly through `agent.run`, so fall back rather than fail.
            warm = False

        if warm:
            verdict = _warm_verdict(
                extractor,
                session=session,
                context=context,
                payload=payload,
                candidate=candidate,
                key=key,
            )
        else:
            verdict = _cold_verdict(
                extractor,
                session=session,
                payload=payload,
                candidate=candidate,
                urls=urls,
                allow_search=allow_search,
            )

        decision = {
            "key": key,
            "candidate_id": candidate.id,
            "outcome": verdict.tool_name or verdict.outcome,
            "steps": verdict.steps,
            "prompt_tokens": verdict.prompt_tokens,
            "completion_tokens": verdict.completion_tokens,
            "cache_hit_tokens": verdict.cache_hit_tokens,
            "cache_miss_tokens": verdict.cache_miss_tokens,
            "routed": False,
            "note": verdict.note,
            "via": verdict.via,
        }

        def finish(routed: int | None) -> int | None:
            decision["routed"] = routed is not None
            if on_decision:
                # Guarded: `finish` runs *after* the audit line is written, so a
                # raising callback used to propagate into `upsert_record`, which
                # catches it and inserts the row — leaving a note on the candidate
                # claiming a route that never happened, plus the duplicate.
                try:
                    on_decision(decision)
                except Exception:
                    log.warning("identity arbiter's on_decision failed", exc_info=True)
            return routed

        answer = verdict.answer
        if not verdict.answered or verdict.tool_name != "same_site":
            decision["note"] = str(answer.get("reason") or verdict.note or "").strip()
            return finish(None)

        decision["note"] = str(answer.get("reason") or "").strip()
        try:
            confidence = float(answer.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        decision["confidence"] = confidence

        if confidence < min_confidence:
            decision["outcome"] = f"same_site below {min_confidence:.2f}"
            return finish(None)

        # Geography outranks the model, exactly as it does in `dupresolve`. The
        # arriving record rarely carries coordinates, so this usually cannot fire —
        # but when both sides have them it is a fact check, not an opinion.
        distance = km_apart(candidate, _Located(payload))
        if distance is not None and distance > FAR_APART_KM:
            decision["outcome"] = f"same_site refused: {distance:.0f} km apart"
            return finish(None)

        # Checked against what the model was SHOWN, never against the full stored
        # article: extraction sees `truncate(...)`, and a quote lifted from the
        # omitted middle would otherwise pass a gate that exists to prove the
        # model read the thing it is ruling on.
        if require_quote and not any(
            verbatim(str(answer.get("quote") or ""), text)[0] for text in verdict.articles.values()
        ):
            decision["outcome"] = "same_site refused: no quote from the article it was shown"
            return finish(None)

        # Written into the row's notes, in the plain prose `_merge_notes` never
        # regenerates, because a routing decision that leaves no trace is
        # indistinguishable later from the row having always been this way.
        from tracker.logic import record_decision

        record_decision(
            candidate,
            "identity",
            f"routed an arriving record (`{key}`) here instead of creating a row",
            by=f"agent ({confidence:.2f})",
            detail=_one_line(decision["note"]),
        )
        return finish(candidate.id)

    # How the extraction context reaches here without `upsert_record` ever seeing
    # it. `upsert` calls `arbiter(session=, record=, payload=, key=, candidate=)`
    # and says of itself that it "stays free of any model"; binding the context
    # into the callable *before* it gets there keeps that true — the write path
    # still calls one opaque function with the arguments it always used.
    arbitrate.for_article = lambda context: functools.partial(arbitrate, context=context)
    return arbitrate


class _Located:
    """Adapts a payload dict to what `km_apart` reads off a Project."""

    def __init__(self, payload: dict[str, Any]):
        self.lat = payload.get("lat")
        self.lon = payload.get("lon")


__all__ = ["MIN_CONFIDENCE", "RULES", "SYSTEM", "SYSTEM_WARM", "same_site_arbiter"]
