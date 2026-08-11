"""Reasoned judgement about a project, where no document states the answer.

The PRD asks for two things no article contains:

    你还需要分析项目可能遇到的困难
    接下来出现什么信号，才可以证明项目正在继续推进

Those are analysis, not extraction. An article reports what happened; whether a
1 GW load in Northern Virginia is interconnection-constrained is a conclusion an
analyst draws from the facts. The PRD assigns that work explicitly — 分析 — so this
module does it with a reasoning model, given everything the database knows.

**The boundary, and it is enforced in code, not by prompt wording.** The same PRD
says:

    不能直接把AI的回答当作事实。关键数字和结论要尽量找到公司公告、政府文件、
    电力公司资料或可靠媒体进行确认

So inference is permitted for *judgements* and forbidden for *facts*. A model may
conclude "this is probably waiting on a substation"; it may not decide that the
investment is $1.2bn or that the site opens in Q3 2027 — those are 关键数字 and must
come from a document. :data:`INFERABLE` is the whitelist, it contains no
quantitative field, and :func:`_reject_facts` drops anything else the model returns.

Inferred output is stored as its own `inferred:` source, which means:

* `confidence` ignores it entirely, exactly as it ignores `derived:` rows — a
  judgement about a project cannot corroborate a fact about it;
* `gaps.basis` reports the field as `inferred`, so the CLI shows it distinctly
  from a reported or an unconfirmed value;
* the model's own confidence is recorded beside it, unscaled, because a number a
  model assigns to its own reasoning is a hint and not a measurement.
"""  # noqa: RUF002 - the PRD is quoted verbatim

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any, Final

from tracker.vocab import RISK_CATEGORIES, RISK_SEVERITIES

if TYPE_CHECKING:
    from tracker.llm import Extractor
    from tracker.models import Project

log = logging.getLogger(__name__)

#: The only things a model may conclude. Deliberately no quantity, date or party:
#: the PRD requires 关键数字 to be confirmed against a document, and a field that
#: could be looked up must never be guessed instead.
INFERABLE: Final[frozenset[str]] = frozenset({"likely_obstacles", "next_signals"})

#: Below this, the model is telling us it is speculating. Kept rather than
#: discarded would clutter the row with noise, so it is dropped and counted.
MIN_CONFIDENCE: Final = 0.35

#: Ceiling on judgements taken from one call, most confident first. A model asked
#: for obstacles will happily enumerate every category if allowed.
MAX_PER_KIND: Final = 3


@dataclass(frozen=True)
class InferredRisk:
    """An obstacle the model believes is likely, with its reasoning."""

    category: str
    severity: str
    reasoning: str
    confidence: float


@dataclass(frozen=True)
class InferredSignal:
    """An observable event that would show the project is still advancing."""

    signal: str
    reasoning: str
    confidence: float


@dataclass
class Analysis:
    """One reasoning pass over one project."""

    project_id: int
    obstacles: list[InferredRisk] = dc_field(default_factory=list)
    signals: list[InferredSignal] = dc_field(default_factory=list)
    rejected: list[str] = dc_field(default_factory=list)
    model: str = ""

    @property
    def empty(self) -> bool:
        return not self.obstacles and not self.signals


def _reject_facts(payload: dict[str, Any]) -> list[str]:
    """Names the model returned that it is not allowed to conclude.

    The prompt asks only for obstacles and signals, but a prompt is a request. If a
    future model helpfully volunteers `investment_usd`, it must be dropped and the
    attempt recorded — silently ignoring it would leave nobody aware that the model
    is trying to assert facts.
    """
    return sorted(k for k in payload if k not in INFERABLE)


def _clean_confidence(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= value <= 1.0:
        return None
    return value


def parse_analysis(project_id: int, payload: dict[str, Any], *, model: str = "") -> Analysis:
    """Validate a reasoning reply into an Analysis. Pure, no I/O."""
    body = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else payload
    if not isinstance(body, dict):
        return Analysis(project_id=project_id, model=model)

    analysis = Analysis(project_id=project_id, model=model, rejected=_reject_facts(body))

    for raw in body.get("likely_obstacles") or []:
        if not isinstance(raw, dict):
            continue
        category = str(raw.get("category") or "").strip().lower()
        severity = str(raw.get("severity") or "").strip().lower()
        reasoning = str(raw.get("reasoning") or "").strip()
        confidence = _clean_confidence(raw.get("confidence"))
        if category not in RISK_CATEGORIES or severity not in RISK_SEVERITIES:
            continue
        if not reasoning or confidence is None or confidence < MIN_CONFIDENCE:
            continue
        analysis.obstacles.append(InferredRisk(category, severity, reasoning, confidence))

    for raw in body.get("next_signals") or []:
        if not isinstance(raw, dict):
            continue
        signal = str(raw.get("signal") or "").strip()
        reasoning = str(raw.get("reasoning") or "").strip()
        confidence = _clean_confidence(raw.get("confidence"))
        if not signal or confidence is None or confidence < MIN_CONFIDENCE:
            continue
        analysis.signals.append(InferredSignal(signal, reasoning, confidence))

    analysis.obstacles.sort(key=lambda r: -r.confidence)
    analysis.signals.sort(key=lambda s: -s.confidence)
    del analysis.obstacles[MAX_PER_KIND:]
    del analysis.signals[MAX_PER_KIND:]
    return analysis


def build_context(project: Project, standing) -> dict[str, str]:
    """Everything the model is allowed to reason from, as prompt variables.

    Deliberately includes what the database could NOT find. A gap is itself
    evidence: a project announced three years ago with no interconnection agreement
    and no expected-online date is telling you something about why.
    """
    from tracker.gaps import MISSING, for_project
    from tracker.tracks import TRACK_LABELS

    def show(value: Any) -> str:
        return "unknown" if value is None else str(value)

    reached: list[str] = []
    missing: list[str] = []
    for state in standing.tracks:
        label = TRACK_LABELS[state.track]
        for milestone in state.reached:
            reached.append(f"  - {milestone} ({label})")
        nxt = state.next_milestone
        if nxt:
            missing.append(f"  - {nxt} ({label})")

    events = {e.event_type: e.event_date for e in getattr(project, "events", ()) or ()}
    dated = [
        f"  - {t} on {events[t]}" for t in events if any(t in s.reached for s in standing.tracks)
    ]

    risks = [
        f"  - {r.category} ({r.severity}): {r.summary}"
        for r in getattr(project, "risks", ()) or ()
        if getattr(r, "status", "open") == "open"
    ]
    gaps = [f"  - {s.field}" for s in for_project(project) if s.status == MISSING]

    return {
        "name": show(project.name),
        "company": show(project.company),
        "customer": show(project.customer),
        "location": f"{project.city or project.county or 'unknown'}, {project.state}",
        "county": show(project.county),
        "state": show(project.state),
        "mw_planned": show(project.mw_planned),
        "mw_built": show(project.mw_built),
        "investment_usd": show(project.investment_usd),
        "first_announced": show(project.first_announced),
        "expected_online": show(project.expected_online),
        "phase": show(project.phase),
        "milestones": "\n".join(dated or reached) or "  (none recorded)",
        "missing_milestones": "\n".join(missing) or "  (none outstanding)",
        "known_risks": "\n".join(risks) or "  (none recorded)",
        "gaps": "\n".join(gaps) or "  (nothing missing)",
    }


def analyse(
    project: Project,
    *,
    extractor: Extractor,
    prompt_name: str = "infer-v1",
    max_tokens: int | None = None,
) -> Analysis:
    """Ask a reasoning model what is obstructing this project and what to watch.

    Raises nothing on a useless reply: an empty Analysis is a valid outcome and the
    caller stores nothing.

    `max_tokens` defaults to None so the extractor applies
    `Settings.max_completion_tokens`. It used to hardcode 4096, which stopped being
    safe the moment this tier began reasoning: the budget is spent on deliberation
    before the answer starts, and the failure here is silent — an empty Analysis is
    indistinguishable from "the model had nothing to say", so a starved panel looks
    like a quiet one. One number to raise, in the one place that documents why.
    """
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt
    from tracker.tracks import standing

    prompt = load_prompt(prompt_name)
    context = build_context(project, standing(project.id, project.events, project.risks))

    try:
        reply = extractor.complete(
            system=prompt.system, user=prompt.render_user(**context), max_tokens=max_tokens
        )
    except LLMError as exc:
        log.warning("inference failed for project %s: %s", project.id, exc)
        return Analysis(project_id=project.id)

    try:
        payload = parse_json_object(reply.text)
    except (LLMJsonError, ValueError) as exc:
        log.warning("inference for project %s returned unusable JSON: %s", project.id, exc)
        return Analysis(project_id=project.id)

    analysis = parse_analysis(project.id, payload, model=reply.model)
    if analysis.rejected:
        # The model tried to assert something it is not permitted to conclude.
        log.warning(
            "inference for project %s tried to return %s; dropped (facts must come "
            "from a document, not a model)",
            project.id,
            ", ".join(analysis.rejected),
        )
    return analysis


__all__ = [
    "INFERABLE",
    "MAX_PER_KIND",
    "MIN_CONFIDENCE",
    "Analysis",
    "InferredRisk",
    "InferredSignal",
    "analyse",
    "build_context",
    "parse_analysis",
]
