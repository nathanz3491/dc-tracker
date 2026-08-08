"""Settling the obstacles nobody could quote.

`tracker risks` ends with a line that has bothered every reader of it:

    36 of 123 are 待确认 — reported by a source with no quote that stands up.
    They are counted above.

Both halves are honest and together they are unsatisfying. The obstacle is counted
in the exposure numbers because a source really did report it, and it is marked
待确认 because the evidence gate could not find a sentence in the article that says
so. Nobody has ever gone back to check which of the two readings is right, and
until now there was no command that could.

**What this does.** For each unconfirmed obstacle it puts the whole article back in
front of a model — not the excerpt, the article — together with the project, the
obstacle as recorded, and every other obstacle on the row, and asks one question:
does this article actually report this obstacle, and if so, which sentence says it?

**What makes the answer trustworthy is not the model.** A returned quote is
accepted only if it is verbatim in the article, checked with the same matcher the
extraction path's evidence gate uses (`crawl._verbatim_run`), and only if the
sentence carries wording for the category it is filed under
(`crawl._risk_quote_supports`). A model that paraphrases, or that quotes a real
sentence about the wrong thing, is refused exactly as an extraction would be. So
the worst outcome is that an obstacle stays 待确认 — the state it is already in.

Three verdicts, and all three are useful:

* **confirmed** — the sentence exists. The quote is written onto the risk and the
  `unconfirmed` marker is cleared, so it stops being counted as unevidenced.
* **refuted** — the article does not report this obstacle at all. The risk is
  marked `superseded`, which drops it out of the open counts without deleting the
  record of having believed it.
* **unclear** — left exactly as it was. This is the honest majority answer for a
  short excerpt or a page that has since been rewritten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import Project, Risk, Source
from tracker.vocab import OPEN_RISK_STATUS

log = logging.getLogger(__name__)

#: Room for a reasoning model to read a whole article before answering.
MAX_TOKENS: Final = 6000

#: Characters of the article shown. Long enough for a full news piece; short
#: enough that a 40-page filing does not become one prompt.
ARTICLE_BUDGET: Final = 24_000

#: Below this the model is guessing and the obstacle stays as it was.
MIN_CONFIDENCE: Final = 0.65

#: What a refuted obstacle becomes. Not a delete: the row records that a source
#: was read this way once, and the next crawl of the same article would otherwise
#: recreate it with nothing to say it had already been rejected.
REFUTED_STATUS: Final = "superseded"


@dataclass
class Judgement:
    """One model reading of one obstacle."""

    risk_id: int
    verdict: str  # "confirmed" | "refuted" | "unclear" | "error"
    quote: str = ""
    reason: str = ""
    confidence: float = 0.0
    #: Set when a quote was offered and refused, naming why. This is the field
    #: that distinguishes "the model paraphrased" from "the article says nothing".
    rejected_quote: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "risk_id": self.risk_id,
            "verdict": self.verdict,
            "quote": self.quote,
            "reason": self.reason,
            "confidence": round(self.confidence, 2),
            "rejected_quote": self.rejected_quote,
        }


@dataclass
class Outcome:
    """What happened to one obstacle, after the write."""

    risk_id: int
    project_id: int
    project: str
    category: str
    severity: str
    summary: str
    judgement: Judgement
    #: "confirmed" | "refuted" | "unclear" | "no_article" | "error"
    result: str = "unclear"
    url: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "risk_id": self.risk_id,
            "project_id": self.project_id,
            "project": self.project,
            "category": self.category,
            "severity": self.severity,
            "summary": self.summary,
            "result": self.result,
            "url": self.url,
            **self.judgement.as_json(),
        }


def unconfirmed_risks(
    session: Session,
    *,
    project_id: int | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> list[Risk]:
    """Open obstacles with no quote that stands up, worst first.

    Ordered by severity then by the capacity behind them, because an unevidenced
    `blocking` obstacle on a gigawatt campus is doing more damage to the exposure
    numbers than a `watch` on a 12 MW site.
    """
    from tracker.vocab import severity_rank

    stmt = (
        select(Risk, Project)
        .join(Project, Risk.project_id == Project.id)
        .where(Risk.status == OPEN_RISK_STATUS)
        .where(Risk.unconfirmed.is_not(None))
    )
    if project_id is not None:
        stmt = stmt.where(Risk.project_id == project_id)
    if category:
        stmt = stmt.where(Risk.category == category)
    rows = session.execute(stmt).all()
    rows.sort(key=lambda pair: (-severity_rank(pair[0].severity), -(pair[1].mw_planned or 0.0)))
    risks = [risk for risk, _ in rows]
    return risks[:limit] if limit else risks


def article_for(source: Source | None, *, cache_dir: Path | None = None) -> str:
    """The full text behind a source, from the article cache, else its excerpt.

    The cache is what makes this worth doing at all. An excerpt is a few hundred
    characters chosen by the extraction that already failed to find the sentence;
    asking a second model to re-read the same fragment would mostly reproduce the
    first answer.
    """
    if source is None:
        return ""
    if cache_dir is None:
        from tracker.config import install_root

        cache_dir = install_root() / ".cache" / "articles"
    from tracker.ingest.fetch import cache_path

    path = cache_path(source.url, Path(cache_dir))
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - unreadable cache file
            log.warning("could not read %s: %s", path, exc)
            text = ""
        if text.strip():
            return text[:ARTICLE_BUDGET]
    return (source.excerpt or "")[:ARTICLE_BUDGET]


def build_context(
    project: Project, risk: Risk, source: Source | None, article: str
) -> dict[str, str]:
    """Everything the model is allowed to reason from, as prompt variables.

    The sibling obstacles are included deliberately. Half the unconfirmed rows on
    the live database are `quote_off_target` — a real sentence filed under the
    wrong category — and a model cannot recognise that failure without seeing what
    the other categories on the row already say.
    """
    siblings = [
        f"  - {r.category} ({r.severity}): {r.summary}"
        for r in getattr(project, "risks", ()) or ()
        if r.id != risk.id and r.status == OPEN_RISK_STATUS
    ]
    return {
        "project_id": str(project.id),
        "company": project.company or "unknown",
        "name": project.name or "unknown",
        "location": f"{project.city or project.county or 'unknown'}, {project.state}",
        "phase": project.phase or "unknown",
        "mw_planned": "unknown" if project.mw_planned is None else f"{project.mw_planned:g}",
        "category": risk.category,
        "severity": risk.severity,
        "summary": risk.summary,
        "why_unconfirmed": risk.unconfirmed or "no reason recorded",
        "offered_quote": (risk.quote or "").strip() or "(none was offered)",
        "siblings": "\n".join(siblings) or "  (none)",
        "url": (source.url if source else "") or "unknown",
        "publisher": (source.source_type if source else "") or "unknown",
        "article": article or "(the article text is not available)",
    }


def judge(
    project: Project,
    risk: Risk,
    source: Source | None,
    *,
    extractor,
    cache_dir: Path | None = None,
    prompt_name: str = "risk-confirm-v1",
) -> tuple[Judgement, str]:
    """Ask a model whether the article reports this obstacle. Returns (judgement, article)."""
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    article = article_for(source, cache_dir=cache_dir)
    if not article.strip():
        return Judgement(risk.id, "error", reason="no article text is available to read"), ""

    prompt = load_prompt(prompt_name)
    try:
        reply = extractor.complete(
            system=prompt.system,
            user=prompt.render_user(**build_context(project, risk, source, article)),
            max_tokens=MAX_TOKENS,
        )
    except LLMError as exc:
        log.warning("risk confirmation failed for risk %s: %s", risk.id, exc)
        return Judgement(risk.id, "error", reason=str(exc)[:160]), article

    try:
        payload = parse_json_object(reply.text)
    except (LLMJsonError, ValueError):
        return Judgement(risk.id, "error", reason="unusable reply"), article

    verdict = str(payload.get("verdict") or "").strip().lower()
    quote = str(payload.get("quote") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if verdict not in {"confirmed", "refuted", "unclear"}:
        verdict = "unclear"
    return Judgement(risk.id, verdict, quote=quote, reason=reason, confidence=confidence), article


def verify_quote(quote: str, article: str, category: str) -> tuple[str, str]:
    """Return `(accepted quote, refusal reason)`. Exactly one is non-empty.

    The same two gates the extraction path applies, in the same order and with the
    same helpers — a confirmation that used a looser test than the gate it is
    overturning would be a way to launder a paraphrase into a citation.
    """
    from tracker.ingest.crawl import _risk_quote_supports, _verbatim_run

    if not quote:
        return "", "no quote offered"
    run = _verbatim_run(quote, article)
    if not run.text:
        return "", "that sentence is not in the article"
    if not _risk_quote_supports(category, run.text):
        return "", f"the sentence does not state a `{category}` obstacle"
    return run.text, ""


def apply_judgement(risk: Risk, judgement: Judgement, article: str) -> str:
    """Write the verdict onto the risk. Returns the result code.

    Nothing is written on `unclear`, and nothing is deleted on `refuted`: the row
    becomes `superseded`, which is how every other path here retires a claim
    without pretending it was never made.
    """
    if judgement.verdict == "error":
        return "error"
    if judgement.confidence < MIN_CONFIDENCE:
        judgement.reason = (
            f"{judgement.reason} (confidence {judgement.confidence:.2f} is below "
            f"the {MIN_CONFIDENCE} floor)"
        ).strip()
        return "unclear"

    if judgement.verdict == "confirmed":
        accepted, refusal = verify_quote(judgement.quote, article, risk.category)
        if not accepted:
            judgement.rejected_quote = judgement.quote
            judgement.quote = ""
            judgement.reason = f"{judgement.reason} — refused: {refusal}".strip(" —")
            return "unclear"
        risk.quote = accepted[:500]
        risk.unconfirmed = None
        judgement.quote = accepted
        return "confirmed"

    if judgement.verdict == "refuted":
        risk.status = REFUTED_STATUS
        return "refuted"
    return "unclear"


def confirm(
    session: Session,
    risks: list[Risk],
    *,
    extractor,
    cache_dir: Path | None = None,
    apply: bool = True,
    on_each=None,
) -> list[Outcome]:
    """Read each obstacle's article and settle it. One model call per obstacle.

    Args:
        apply: False judges and writes nothing, so a run can be previewed at full
            cost but zero risk. The judgement is identical either way — the same
            code decides, and only the assignment is skipped.
        on_each: called with each Outcome as it lands, for progress output.
    """
    sources = {s.id: s for s in session.scalars(select(Source)).all()}
    out: list[Outcome] = []
    for risk in risks:
        project = session.get(Project, risk.project_id)
        if project is None:
            continue
        source = sources.get(risk.source_id)
        judgement, article = judge(project, risk, source, extractor=extractor, cache_dir=cache_dir)
        outcome = Outcome(
            risk_id=risk.id,
            project_id=project.id,
            project=f"{project.company} — {project.name}",
            category=risk.category,
            severity=risk.severity,
            summary=risk.summary,
            judgement=judgement,
            url=(source.url if source else ""),
        )
        if not article and judgement.verdict == "error":
            outcome.result = "no_article"
        elif apply:
            outcome.result = apply_judgement(risk, judgement, article)
        else:
            # Judge exactly as `apply` would, against a throwaway copy, so a
            # preview cannot report an outcome the real run would not produce.
            probe = _Detached(risk.category, risk.quote, risk.unconfirmed, risk.status)
            outcome.result = apply_judgement(probe, judgement, article)  # type: ignore[arg-type]
        if on_each is not None:
            on_each(outcome)
        out.append(outcome)
    if apply:
        session.flush()
    return out


@dataclass
class _Detached:
    """A stand-in risk for `--dry-run`, with the four attributes the write touches."""

    category: str
    quote: str | None
    unconfirmed: str | None
    status: str


__all__ = [
    "ARTICLE_BUDGET",
    "MIN_CONFIDENCE",
    "REFUTED_STATUS",
    "Judgement",
    "Outcome",
    "apply_judgement",
    "article_for",
    "build_context",
    "confirm",
    "judge",
    "unconfirmed_risks",
    "verify_quote",
]
