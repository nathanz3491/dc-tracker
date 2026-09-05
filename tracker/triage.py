"""Settling a contradiction by letting a model read the sources and rule on them.

The agent-backed replacement for `logic.decide` and `dupresolve.resolve_one`, and
it exists because both of those were capped by their own shape rather than by the
model's judgement. Measured on the live database, twice:

* `logic.decide` returns "nothing to choose between" **before calling the model**
  whenever `ACTIONS[code]` is empty, and it is empty for 11 of 16 codes — 432 of
  526 findings, 334 of them about tranches. Of the 94 it *was* shown, it acted on
  52 and declined 10. The model was never the cautious party.
* `dupresolve.merge_blocked` refuses a pair whose only evidence is a
  cross-granularity key match, regardless of what the model concluded. It ruled 45
  pairs "same" at 0.80-0.85 with containment reasoning of its own — *"El Mirage is
  a city within that county"* — and every one was left unactionable.

**The repair is a claim, never a column.** This is the whole design and it is not
a preference. A project scalar is a cache of the claim set: `recompute_from_sources`
re-derives it on the next ingest, merge or `backfill derive`, and `tracker init`
runs on every deploy. Measured directly — clear `mw_built` on #14, commit, derive,
and 230.0 comes back. Every field-assigning action in `logic.py` is therefore
transient, which is why `no_inversions` sat at exactly 30 failures across a run
that resolved `built_exceeds_planned` 18 times. `audit.py` learned this and
reshaped around `conflicts.supersede`; this module starts there.

So the model is not asked "what should this field be". It is asked **which
citations are wrong about it**, which is a question about evidence it can actually
answer from the article, and the answer survives every recompute because the merge
policy then derives the field from the claims that are left.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

log = logging.getLogger(__name__)

#: Fields a model may rule a claim out of. Deliberately not every writable field:
#: `name`, `company` and the locality fields are identity, never overwritten once
#: set even by the ingest path, so superseding a claim about them changes nothing
#: and would only look like it had.
RULEABLE_FIELDS: frozenset[str] = frozenset(
    {
        "mw_planned",
        "mw_built",
        "investment_usd",
        "first_announced",
        "expected_online",
        "customer",
        "phase",
    }
)


@dataclass
class Outcome:
    """What one triage run concluded and what it changed."""

    #: "ruled" — claims superseded and the row re-derived.
    #: "left" — the model read the evidence and declined to act, with a reason.
    #: "unusable" — it answered in a shape that could not be applied.
    #: "error" — the run never reached an answer.
    verdict: str
    note: str = ""
    changes: list[str] = dc_field(default_factory=list)
    confidence: float = 0.0
    steps: list[str] = dc_field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def acted(self) -> bool:
        return self.verdict == "ruled" and bool(self.changes)


# --- the tools that end a run ----------------------------------------------


def rule_out_tool() -> Any:
    """Terminal tool: name the citations that are wrong about a field.

    One field per call by design. A model asked to fix three fields at once
    produces one confidence for three separate judgements, and the weakest of them
    then rides in on the strongest.
    """
    from tracker.agent import Tool

    return Tool(
        name="rule_out_claims",
        description=(
            "Rule one or more citations' claims about ONE field out of the merge, "
            "because you have read the article and the claim does not support the "
            "value. The field is then re-derived from whatever claims remain — you "
            "are not choosing the new value, you are removing wrong evidence. "
            "Quote the sentence that convinced you, verbatim from the article."
        ),
        parameters={
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": sorted(RULEABLE_FIELDS),
                    "description": "The one field these citations are wrong about.",
                },
                "source_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Citation ids whose claim about `field` is wrong.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why, in one sentence a reader can check.",
                },
                "quote": {
                    "type": "string",
                    "description": (
                        "A whole sentence, verbatim from one of the articles you read, "
                        "that shows the claim is wrong. Must be at least 40 characters "
                        "— a fragment like '250 MW' appears in every article about a "
                        "250 MW site and evidences nothing."
                    ),
                },
                "confidence": {"type": "number", "description": "0 to 1."},
            },
            "required": ["field", "source_ids", "reason", "confidence"],
        },
        terminal=True,
    )


def leave_alone_tool() -> Any:
    """Terminal tool: the evidence does not settle it, and saying so is an answer."""
    from tracker.agent import Tool

    return Tool(
        name="leave_alone",
        description=(
            "The evidence you read does not settle this. Say what you found and why "
            "it is not enough. This is a real answer and is recorded as one — it is "
            "always better than a guess."
        ),
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
        terminal=True,
    )


# --- applying what it decided ----------------------------------------------


def _articles_read(result: Any) -> dict[str, str]:
    """url -> text, for every article the run actually read.

    The quote is checked against what the model was *shown*, not against a fresh
    fetch. Re-fetching would let a page that changed between the read and the check
    reject a quote that was honest when it was made.
    """
    out: dict[str, str] = {}
    for step in result.steps:
        if step.name == "read_article" and not step.failed:
            url = str(step.arguments.get("url") or "")
            if url:
                out[url] = step.result
    return out


def apply_rule_out(
    session: Any,
    project: Any,
    answer: dict[str, Any],
    *,
    articles: dict[str, str],
    min_confidence: float = 0.0,
    require_quote: bool = True,
) -> tuple[bool, str, str]:
    """Supersede the named claims and re-derive. Returns (acted, sentence, refusal).

    The field is emptied before the recompute rather than assigned after it, which
    is `audit._rule_against`'s trick and worth restating: `upsert.resolve` returns
    the existing value when no claim participates, so a field whose every claim has
    been ruled out stays empty — durably, and without this function ever choosing a
    number.
    """
    from tracker.conflicts import MISREAD, supersede
    from tracker.upsert import recompute_from_sources

    name = str(answer.get("field") or "").strip()
    if name not in RULEABLE_FIELDS:
        return False, "", f"{name!r} is not a field a claim may be ruled out of"

    try:
        confidence = float(answer.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < min_confidence:
        return False, "", f"confidence {confidence:.2f} is below {min_confidence:.2f}"

    wanted = {int(i) for i in (answer.get("source_ids") or []) if str(i).strip().isdigit()}
    if not wanted:
        return False, "", "no citation ids given"
    rows = [s for s in project.sources if s.id in wanted]
    if not rows:
        return False, "", f"none of {sorted(wanted)} is a citation on this row"

    # A claim can only be ruled out of the merge if it is in the merge. Silently
    # superseding a field the source never claimed reports a repair that changed
    # nothing, which is the failure this whole module exists to stop.
    claiming = []
    for source in rows:
        try:
            claims = json.loads(source.claims or "{}")
        except (TypeError, ValueError):
            claims = {}
        if isinstance(claims, dict) and claims.get(name) is not None:
            claiming.append(source)
    if not claiming:
        return False, "", f"none of those citations claims {name}"

    if require_quote:
        from tracker.agent import verbatim

        quote = str(answer.get("quote") or "")
        accepted = ""
        for source in claiming:
            text = articles.get(source.url, "") or (source.excerpt or "")
            accepted, _refusal = verbatim(quote, text)
            if accepted:
                break
        if not accepted:
            # Try every article the run read, not only the ruled-against ones: the
            # sentence that disproves source A is very often published by B.
            for text in articles.values():
                accepted, _refusal = verbatim(quote, text)
                if accepted:
                    break
        if not accepted:
            return False, "", "the quote is not in any article this run read"

    from tracker.upsert import DEFAULT_PHASE

    was = getattr(project, name, None)
    # `misread`, not `superseded`, because that is the question this module's prompt
    # actually puts to the model: "which citations are WRONG about it" — a scope or
    # unit error, a figure about another building. `superseded` means the opposite
    # about time ("right in 2024, restated since"), and filing a misread under it is
    # what made these rulings look like they bound forever on a mutable question.
    # Both leave the merge and both survive a re-crawl; only a reader can tell them
    # apart, and now can.
    marked = sum(1 for source in claiming if supersede(source, name, reason=MISREAD))
    # Blanked before the recompute, not assigned after: `upsert.resolve` returns
    # the existing value when no claim participates, so a field whose every claim
    # is ruled out stays empty without this code choosing a value.
    #
    # `phase` is the exception and it cost three of five rounds on the first
    # overnight run. It is the one NOT NULL column here, so `= None` and a flush
    # raised IntegrityError before the recompute could refill it — and because the
    # exception escaped mid-batch, the whole logic phase of rounds 1, 2 and 3 died
    # after the first `phase` ruling. `DEFAULT_PHASE` is what `recompute_from_
    # sources` itself falls back to two lines from the end, so this is the same
    # answer arrived at without going through an illegal state.
    setattr(project, name, DEFAULT_PHASE if name == "phase" else None)
    session.flush()
    recompute_from_sources(session, project)
    now = getattr(project, name, None)
    sentence = (
        f"{name} {was} -> {now} "
        f"({marked} claim(s) superseded on citation(s) {sorted(s.id for s in claiming)})"
    )
    return True, sentence, ""


# --- the run ----------------------------------------------------------------

SYSTEM = """You settle contradictions in a database of US data centre projects.

Every stored value must trace to a sentence somebody published. Your job is not to
decide what a figure should be — it is to find out which citations are WRONG about
it, and rule those claims out. The database then re-derives the field from the
claims that survive.

How to work:
1. `show_project` to see the row, then `list_sources` for its citations.
2. `read_article` the ones that matter. The stored excerpt is a fragment; the
   article is where the answer is. Read more than one.
3. `search_web` only if the stored sources genuinely cannot settle it, then
   `read_article` a result so you can quote it.
4. Then call `rule_out_claims` or `leave_alone`.

The commonest real cause of these contradictions is SCOPE: a figure that describes
one building, or a whole programme, stored as if it described this campus. A
"total" in an article about a campus expansion is often the campus, not the
expansion. Watch for it.

Quote a WHOLE SENTENCE, copied exactly. A quote under 40 characters is rejected,
and so is a paraphrase. If you cannot quote it, you cannot rule on it — call
`leave_alone` and say what you found. Declining is a respected answer; guessing is
not."""


def triage(
    session: Any,
    project: Any,
    *,
    question: str,
    extractor: Any,
    min_confidence: float = 0.0,
    require_quote: bool = True,
    allow_search: bool = True,
    max_steps: int | None = None,
    on_step: Any = None,
) -> Outcome:
    """Let a model read the sources and rule on one contradiction.

    `question` is the finding in the reader's terms — the caller owns it, because
    `logic` and `audit` describe their findings differently and neither should have
    to phrase things the other's way.
    """
    from tracker import agent

    tools = agent.evidence_toolkit(session, allow_search=allow_search)
    tools += [rule_out_tool(), leave_alone_tool()]

    kwargs: dict[str, Any] = {}
    if max_steps is not None:
        kwargs["max_steps"] = max_steps
    result = agent.run(
        f"Project #{project.id}: {project.name} — {project.company}\n\n{question}",
        tools=tools,
        extractor=extractor,
        system=SYSTEM,
        on_step=on_step,
        **kwargs,
    )

    out = Outcome(
        verdict="error",
        note=result.note,
        steps=result.tool_names,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cache_hit_tokens=result.cache_hit_tokens,
        cache_miss_tokens=result.cache_miss_tokens,
    )
    if not result.answered:
        # "stopped" and "exhausted" are not errors in the provider sense, and the
        # note is the model's own account of why — worth keeping verbatim.
        out.verdict = "left" if result.outcome in {"stopped", "exhausted"} else "error"
        return out

    answer = result.answer or {}
    if result.tool_name == "leave_alone":
        out.verdict = "left"
        out.note = str(answer.get("reason") or "").strip()
        return out

    try:
        out.confidence = float(answer.get("confidence") or 0)
    except (TypeError, ValueError):
        out.confidence = 0.0

    acted, sentence, refusal = apply_rule_out(
        session,
        project,
        answer,
        articles=_articles_read(result),
        min_confidence=min_confidence,
        require_quote=require_quote,
    )
    if not acted:
        out.verdict = "unusable"
        out.note = refusal
        return out

    out.verdict = "ruled"
    out.changes = [sentence]
    out.note = str(answer.get("reason") or "").strip()
    return out


# --- the same question, asked about a pair of rows --------------------------

#: The judgement, framed as a search for contradictions rather than for resemblance.
#: Shared verbatim by both pair judges and by the ingest-time arbiter, which is why it
#: is a constant and not three paragraphs that drift.
#:
#: **Why this framing.** OpenSanctions Pairs (arXiv 2603.11051) measured entity
#: matching by prompt shape over 755,540 labelled pairs: asking a model to look for
#: CONTRADICTORY evidence and to answer positive only when it found none took an open
#: 14B model from a 91.3% rule baseline to 98.2% F1. Asking "are these the same"
#: invites a similarity judgement, and two data centre rows in one town always
#: resemble each other.
#:
#: **Why it is worth changing here specifically.** Measured on this database
#: (`scripts/eval_pairs.py`, recorded in `docs/duplicate-shapes.md`): of the seven
#: pairs a judge has ruled `different`, six have no rail in
#: `dupresolve.evidence_blocks_merge` that refuses them. On that population the rails
#: contribute nothing and the judgement is the whole decision, so the wording of the
#: question is not a detail.
#:
#: Deliberately tool-agnostic: the three sites end in `rule_same`, `same_site` and a
#: JSON `"same"` respectively, so this names neither.
CONTRADICTIONS = """HOW TO DECIDE: look for what makes these two DIFFERENT, not for what makes them
look alike. Two data centre rows in one town always look alike — same industry, same
vocabulary, often the same metro in the name. Resemblance is not evidence.

Work through what would rule a match OUT:
  * the stored coordinates are far apart — the distance is given to you, and a large
    one is decisive on its own;
  * the articles name two different street addresses, parcels, substations or
    serving utilities;
  * one article describes both as separate developments, or lists them side by side;
  * the operator, the named customer AND the tranche names all differ — any one of
    those alone is weak, because one campus has several parties attached;
  * the names differ only by a trailing number. `Polaris Forge 1` and `Polaris
    Forge 2` are two campuses, not one stored twice.

And what would tie them together. Only these count:
  * a street address, parcel or substation in common;
  * a "formerly known as", "also known as", or a rename;
  * the same tranche or building name;
  * one row's company named in the other's article as the builder, landlord or
    tenant of the same site.

A campus can sit outside the town it is named for, and often does — a site called
after the nearest city may be filed under the next county along. So a town and a
county that do not contain each other is NOT by itself a contradiction; look for a
street address or a substation before concluding anything from place names.

Then answer. Same site: only when you searched for contradictions, found none, and
can quote one tie from the list above. Different sites: when you can quote or were
given one contradiction. Anything else: say you cannot settle it, which is a real
answer and is recorded as one."""


PAIR_SYSTEM_BASE = """You decide whether two rows in a data centre database are the same site.

One real campus often has a builder, a landlord and an occupier, and each name
makes its own row — those are three legitimate rows, not duplicates. Two rows are
the same site only when they describe the same physical place.

How to work:
1. `show_project` both rows, and `list_sources` on each.
2. `read_article` the citations that would settle it. Names differ constantly;
   what does not differ is the address, the substation, the tranche names and the
   building count.
3. `find_projects` if you suspect a third row is the same place.
4. `search_web` only if the stored sources cannot settle it.
5. Then `rule_same`, `rule_different`, or `leave_alone`.

The commonest real case is GRANULARITY: one row filed under a city and the other
under the county containing it. That is the same site reported at two precisions —
but only if the city really is in that county. Check it; do not assume it from the
names sounding related.

`rule_same` DELETES a row. There is no undo and no re-crawl recovers it. Quote a
whole sentence that ties the two to one place. If you cannot, use `leave_alone`:
a pair left open costs a report line, and a wrong merge costs two rows."""

#: What the judges actually run. Split from the text above so `scripts/eval_pairs.py`
#: can put the two side by side on the same pairs without keeping a stale copy of the
#: old wording — a measured prompt change needs its own baseline, and a baseline that
#: is a duplicate string goes out of date the first time somebody edits one of them.
PAIR_SYSTEM = PAIR_SYSTEM_BASE + "\n\n" + CONTRADICTIONS


def pair_verdict_tools() -> list[Any]:
    """The three ways a pair judgement can end."""
    from tracker.agent import Tool

    confidence = {"type": "number", "description": "0 to 1."}
    #: Optional on purpose. The rails decide what may happen, not this field, so a
    #: model that omits it is not refused — it simply leaves no account of what it
    #: ruled out, which is what a reader of `duplicates parked` most wants six months
    #: later and never had.
    contradictions = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "What you checked for and what you found, one short phrase each — either a "
            "contradiction ('two addresses: 100 Innovation Dr vs 4500 Beaumont Rd') or "
            "the check coming back clean ('checked substations — none named')."
        ),
    }
    return [
        Tool(
            name="rule_same",
            description=(
                "These two rows are one site. The lower-quality row is folded into the "
                "other and DELETED; every figure is recomputed from the combined "
                "citations. Irreversible."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "quote": {
                        "type": "string",
                        "description": (
                            "A whole sentence, verbatim from an article you read, tying "
                            "these two to one place."
                        ),
                    },
                    "confidence": confidence,
                    "contradictions": contradictions,
                },
                "required": ["reason", "confidence"],
            },
            terminal=True,
        ),
        Tool(
            name="rule_different",
            description=(
                "These are different sites. The pair is parked, which stops `capex` "
                "holding one of the rows out of the published totals. Reversible with "
                "`duplicates unpark`."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "confidence": confidence,
                    "contradictions": contradictions,
                },
                "required": ["reason", "confidence"],
            },
            terminal=True,
        ),
        leave_alone_tool(),
    ]


def _checked(answer: dict[str, Any]) -> list[str]:
    """The `contradictions` a judge reported, cleaned. Absent is not an error.

    Optional by design: the rails decide what may happen to a pair, so a model that
    omits the field is not refused. What it loses is the account of what it ruled
    out, and nothing else.
    """
    raw = answer.get("contradictions") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [" ".join(str(item).split())[:120] for item in raw if str(item).strip()][:6]


def pair_triage(
    session: Any,
    pair: Any,
    *,
    extractor: Any,
    allow_merge: bool = False,
    min_confidence: float = 0.85,
    require_quote: bool = True,
    allow_search: bool = True,
    folded: dict[int, int] | None = None,
    system: str = "",
    on_step: Any = None,
) -> Any:
    """Let a model read both rows' sources and rule on the pair.

    Returns `dupresolve.Decision`, so every caller and printer that already knows
    that shape keeps working.

    **What this drops from `merge_blocked`, and why.** The categorical refusal of a
    cross-granularity pair exists because "no string comparison can tell whether
    'Racine County' and 'Mount Pleasant' are one project. A model is not a string
    comparison, but it is not a person with a map either." That was true of a model
    shown two rows and nothing else. This one can read the articles and search, so
    it *can* be a person with a map — and 28 of 47 live groups are exactly this
    shape, with the model already reasoning containment unprompted.

    **What it keeps — and it is not this function's to decide.** Every other rail
    is a fact about the pair rather than about the judge, so it lives in
    `dupresolve.evidence_blocks_merge` and both judges call it: geography outranks
    the model, `Polaris Forge 1` and `Polaris Forge 2` are two campuses that share
    every other signal, a market-sequence tranche carries no merge authority, and a
    shared name word is a word. This function used to restate two of those by hand
    and silently lacked the other two — its own docstring promised the
    market-sequence refusal and the code never checked it. A confidence floor stays
    because a merge is irreversible, and the *park* has the same floor the one-call
    path has always had: an answer under `dupresolve.MIN_CONFIDENCE` is a shrug,
    and a shrug must not release a row from the capex hold.
    """
    from tracker import agent
    from tracker.dupresolve import (
        MIN_CONFIDENCE,
        Decision,
        Judgement,
        _surviving,
        evidence_blocks_merge,
        pair_label,
        survivor,
    )
    from tracker.logic import one_line
    from tracker.models import Project

    folded = folded if folded is not None else {}
    a_id, b_id = _surviving(pair.a_id, folded), _surviving(pair.b_id, folded)
    got = Decision(a_id=pair.a_id, b_id=pair.b_id, label=pair_label(pair))
    if a_id == b_id:
        got.detail = "one of the rows is gone — merged by an earlier pair in this run"
        return got

    a, b = session.get(Project, a_id), session.get(Project, b_id)
    if a is None or b is None:
        got.detail = "one of the rows is gone"
        return got

    tools = agent.evidence_toolkit(session, allow_search=allow_search) + pair_verdict_tools()
    task = (
        f"Are these the same site?\n\n"
        f"ROW A: #{a.id} {a.name} — {a.company} — "
        f"{a.city or a.county or '?'}, {a.state}\n"
        f"ROW B: #{b.id} {b.name} — {b.company} — "
        f"{b.city or b.county or '?'}, {b.state}\n\n"
        f"Why they were paired: {'+'.join(pair.kinds) or 'locality'}"
        + (f"\nShared tranche keys: {', '.join(pair.shared_blocks)}" if pair.shared_blocks else "")
    )

    result = agent.run(
        task, tools=tools, extractor=extractor, system=system or PAIR_SYSTEM, on_step=on_step
    )
    if not result.answered:
        got.detail = result.note or f"the run ended as {result.outcome} without deciding"
        return got

    answer = result.answer or {}
    reason = str(answer.get("reason") or "").strip()
    checked = _checked(answer)
    try:
        confidence = float(answer.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0

    if result.tool_name == "leave_alone":
        got.detail = reason or "the model would not say"
        return got

    verdict = "same" if result.tool_name == "rule_same" else "different"
    got.judgement = Judgement(verdict, confidence, reason)
    by = f"agent ({confidence:.2f})"

    if confidence < MIN_CONFIDENCE:
        # The same floor `ask_model` applies, for the same reason: a park releases a
        # row from the capex hold, and an answer the model itself rates a coin toss
        # must not be the thing that does it. Left in the report, not parked.
        got.judgement = Judgement(
            verdict, confidence, reason, outcome="declined", note="below the floor"
        )
        got.detail = f"confidence {confidence:.2f} is below the {MIN_CONFIDENCE} floor"
        return got

    if verdict == "different":
        from tracker import pairs as pairs_mod

        classes = "+".join(pair.kinds) or "locality"
        # What it ruled out travels with the decision. `duplicates parked` is read
        # months later by somebody deciding whether to reopen the question, and
        # "different sites" is a far weaker thing to inherit than "different sites;
        # checked substations - none named; two addresses".
        told = f"{reason} [{classes}]"
        if checked:
            told += f" ruled out: {'; '.join(checked)}"
        pairs_mod.park(session, [a.id, b.id], reason=one_line(told), by=by)
        got.action = "parked"
        got.detail = "ruled out — capex will stop holding one of these rows back"
        return got

    if not allow_merge:
        got.detail = "the same site, but merging needs --merge"
        return got
    if confidence < min_confidence:
        got.detail = f"confidence {confidence:.2f} is below the {min_confidence:.2f} a merge needs"
        return got

    # Every rail that is a fact about the pair, shared with the one-call path. The
    # one this judge is allowed past is the cross-granularity refusal, because it
    # read the articles; see `evidence_blocks_merge`.
    blocked = evidence_blocks_merge(pair, a, b, judge_read_the_sources=True)
    if blocked:
        got.detail = blocked
        return got
    if require_quote:
        from tracker.agent import verbatim

        articles = _articles_read(result)
        quote = str(answer.get("quote") or "")
        if not any(verbatim(quote, text)[0] for text in articles.values()):
            got.detail = "no sentence from an article this run read supports a merge"
            return got

    from tracker.logic import record_decision
    from tracker.merge import merge_projects

    keep, gone = survivor(a, b)
    merged = merge_projects(session, keep.id, [gone.id], by=by)
    note = reason + (f" — checked: {'; '.join(checked)}" if checked else "")
    record_decision(keep, "duplicate", f"folded #{gone.id} into this row", by=by, detail=note)
    folded[gone.id] = keep.id
    got.action = "merged"
    got.kept_id = keep.id
    got.removed_ids = list(merged.removed)
    got.detail = (
        f"{merged.sources_moved} citation(s), {merged.events_moved} milestone(s) and "
        f"{merged.risks_moved} obstacle(s) moved onto #{keep.id}"
    )
    return got


def resolve_pairs(
    session: Any,
    *,
    extractor: Any,
    limit: int = 20,
    allow_merge: bool = False,
    weak: bool = False,
    min_confidence: float = 0.85,
    require_quote: bool = True,
    allow_search: bool = True,
    system: str = "",
    on_step: Any = None,
) -> list[Any]:
    """Work the suspected pairs with an agent. Returns `dupresolve.Decision` list.

    Same selection and same return shape as `dupresolve.resolve`, so every printer
    and the `--json` payload keep working — only the judge changes. `folded` is
    threaded for the reason it is there: four rows for one campus produce six
    pairs, and without it the first merge makes the other five report "one of the
    rows is gone" and the operator runs the command again.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from tracker.capex import suspected_duplicates
    from tracker.models import Project

    session.scalars(
        select(Project).options(selectinload(Project.sources), selectinload(Project.blocks))
    ).all()

    found = sorted(suspected_duplicates(session), key=lambda p: (p.rank, p.a_id, p.b_id))
    if not weak:
        # A pair raised only by a shared name word cannot be merged and asking
        # costs a call to be told what the rails already know.
        found = [p for p in found if set(p.kinds) - {"name"}]

    folded: dict[int, int] = {}
    out: list[Any] = []
    for pair in found[:limit]:
        out.append(
            pair_triage(
                session,
                pair,
                extractor=extractor,
                allow_merge=allow_merge,
                min_confidence=min_confidence,
                require_quote=require_quote,
                allow_search=allow_search,
                folded=folded,
                system=system,
                on_step=on_step,
            )
        )
    return out


__all__ = [
    "CONTRADICTIONS",
    "PAIR_SYSTEM",
    "PAIR_SYSTEM_BASE",
    "RULEABLE_FIELDS",
    "SYSTEM",
    "Outcome",
    "apply_rule_out",
    "leave_alone_tool",
    "pair_triage",
    "pair_verdict_tools",
    "rule_out_tool",
    "triage",
]
