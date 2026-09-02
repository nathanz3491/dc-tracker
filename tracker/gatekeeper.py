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

import logging
from typing import Any

log = logging.getLogger(__name__)

#: Below this a route is refused and the row inserts. Deliberately higher than the
#: 0.85 a *merge* of two stored rows needs: a merge is reviewed against two full
#: rows of citations, while this is decided from one arriving article.
MIN_CONFIDENCE: float = 0.9

SYSTEM = """You decide whether an article describes a data centre campus already in a database.

You are called at the moment a new row would be created. If the article is about a
site already held, saying so attaches its citations to that row instead — which is
right, and cheap. If it is a different site, saying so creates the row — also right.

How to work:
1. `show_project` the candidate row, and `list_sources` to see what it already cites.
2. `read_article` the arriving url. This is the evidence that decides it, and no
   string comparison has it.
3. `find_projects` if you suspect a third row is involved.
4. Then `same_site`, `different_site`, or `unsure`.

One real campus routinely has a builder, a landlord and an occupier, and each name
makes its own legitimate row. Two rows are the same site only when they are the same
PHYSICAL PLACE — the same address, parcel, substation or building count.

The commonest true match is GRANULARITY: one row filed under a city and the other
under the county containing it. Check that the city really is in that county rather
than assuming it from the names.

`unsure` is a good answer and costs almost nothing: the row is created and the pair
is reported for review, which is what happens today anyway. Saying `same_site`
wrongly puts two different campuses in one row and cannot be undone. When the
article does not settle it, say `unsure`."""


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
        *, session: Any, record: Any, payload: dict[str, Any], key: str, candidate: Any
    ) -> int | None:
        from tracker import agent
        from tracker.agent import verbatim
        from tracker.dupresolve import FAR_APART_KM, km_apart

        urls = _incoming_urls(record)
        if not urls:
            # A record with no citation is a hand-curated or ISO row. There is no
            # article to read, so there is nothing here this cannot already do.
            return None

        where = ", ".join(
            p for p in (payload.get("city"), payload.get("county"), payload.get("state")) if p
        )
        task = (
            "An article would create a NEW row. Is it the same site as the candidate?\n\n"
            f"ARRIVING, from {urls[0]}\n"
            f"  company:  {payload.get('company') or 'unknown'}\n"
            f"  name:     {payload.get('name') or 'unknown'}\n"
            f"  location: {where or 'unknown'}\n\n"
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

        decision = {
            "key": key,
            "candidate_id": candidate.id,
            "outcome": result.tool_name or result.outcome,
            "steps": result.tool_names,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cache_hit_tokens": result.cache_hit_tokens,
            "cache_miss_tokens": result.cache_miss_tokens,
            "routed": False,
            "note": result.note,
        }

        def finish(routed: int | None) -> int | None:
            decision["routed"] = routed is not None
            if on_decision:
                on_decision(decision)
            return routed

        if not result.answered or result.tool_name != "same_site":
            answer = result.answer or {}
            decision["note"] = str(answer.get("reason") or result.note or "").strip()
            return finish(None)

        answer = result.answer or {}
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

        if require_quote:
            articles = {
                step.arguments.get("url"): step.result
                for step in result.steps
                if step.name == "read_article" and not step.failed
            }
            if not any(
                verbatim(str(answer.get("quote") or ""), text)[0] for text in articles.values()
            ):
                decision["outcome"] = "same_site refused: no quote from an article it read"
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
            detail=decision["note"],
        )
        return finish(candidate.id)

    return arbitrate


class _Located:
    """Adapts a payload dict to what `km_apart` reads off a Project."""

    def __init__(self, payload: dict[str, Any]):
        self.lat = payload.get("lat")
        self.lon = payload.get("lon")


__all__ = ["MIN_CONFIDENCE", "SYSTEM", "same_site_arbiter"]
