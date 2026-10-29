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

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import Project, Risk, Source, utcnow
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

#: How a refusal by the category check begins, in a judgement's reason and so in the
#: decline it is remembered by. `fresh` looks for it; one constant, so the two
#: cannot drift apart.
OFF_CATEGORY: Final = "the sentence does not state"

#: When the category check (`crawl._risk_quote_supports`) last changed what it
#: accepts, in UTC. Move it forward with the next change to that check.
#:
#: An obstacle the model found a sentence for, which the check then refused, was not
#: left undecided by the model — it was decided, and overruled. Once the check has
#: changed that is a question worth paying for again, so `fresh` stops holding it
#: back. On 2026-10-19, 131 of the 159 obstacles held back were exactly these: the
#: check matched raw substrings, and "approved" was not "approval". On 2026-10-20 it
#: was 31 of 40, on plain words the table lacked ("grid connection delays", "the
#: administrative pause", "Planning Board"); this moment is after that night ended
#: (its loop stops by 20:01 UTC), so all of them are read once more.
CATEGORY_CHECK_REVISED_AT: Final = dt.datetime(2026, 10, 20, 20, 5)


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
        from tracker.config import cache_dir

        cache_dir = cache_dir("articles")
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


def evidence(
    risk: Risk,
    source: Source | None,
    *,
    cache_dir: Path | None = None,
    article: str | None = None,
) -> str:
    """Fingerprint of what an obstacle is judged on: the obstacle and its article.

    The article text is hashed whole, so a re-fetch that changed the page — or a
    cache that finally holds it where the excerpt stood in before — is new evidence.
    `article` is the text when the caller has already read it.
    """
    import hashlib

    from tracker import declines

    if article is None:
        article = article_for(source, cache_dir=cache_dir)
    return declines.fingerprint(
        risk.category,
        risk.severity,
        risk.summary,
        risk.quote or "",
        risk.unconfirmed or "",
        source.url if source else "",
        hashlib.sha1(article.encode("utf-8")).hexdigest(),
    )


def sources_for(session: Session, risks: list[Risk]) -> dict[int, Source]:
    """The citations behind these obstacles, by id — and only those.

    `fresh` and `confirm` each loaded the whole `source` table, every citation in
    the database with its claims, to look up the one or two dozen a run reads.
    """
    wanted = {r.source_id for r in risks if r.source_id is not None}
    if not wanted:
        return {}
    return {s.id: s for s in session.scalars(select(Source).where(Source.id.in_(wanted)))}


def held_declines(session: Session) -> dict[str, Any]:
    """The `risk` declines that still hold anything back.

    A decline the category check made rather than the model — the model quoted a
    sentence and the check refused it — is dropped once the check has changed since
    (`CATEGORY_CHECK_REVISED_AT`).
    """
    from tracker import declines

    return {
        subject: row
        for subject, row in declines.load(session, "risk").items()
        if not _overruled_by_old_check(row)
    }


def fresh(
    session: Session,
    risks: list[Risk],
    *,
    cache_dir: Path | None = None,
    again: bool = False,
) -> tuple[list[Risk], int]:
    """The obstacles not already judged on the same article, and how many were.

    `unconfirmed_risks` sorts the same way every time, so without this the same top
    `--limit` obstacles were read every round: 141 open unquoted obstacles on the
    snapshot this was written against, 2 ever refuted, and nothing recording the
    `unclear` answers in between.

    An article is read and hashed only for an obstacle that has a decline to compare
    it with (`declines.pick`); one never judged needs neither.
    """
    from tracker import declines

    sources = sources_for(session, risks)
    kept, held, _ = declines.pick(
        risks,
        held_declines(session),
        lambda r: str(r.id),
        lambda r: evidence(r, sources.get(r.source_id), cache_dir=cache_dir),
        again=again,
    )
    return kept, held


def _overruled_by_old_check(row) -> bool:
    """A decline that is the category check's refusal, made before the check last changed."""
    return (
        f"refused: {OFF_CATEGORY}" in (row.reason or "")
        and row.decided_at is not None
        and row.decided_at < CATEGORY_CHECK_REVISED_AT
    )


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
    article = article_for(source, cache_dir=cache_dir)
    if not article.strip():
        return Judgement(risk.id, "error", reason="no article text is available to read"), ""
    question = Question(risk.id, article, _prompt_user(project, risk, source, article, prompt_name))
    return ask(question, extractor=extractor, prompt_name=prompt_name), article


@dataclass(frozen=True)
class Question:
    """One obstacle's question as plain values, so it can be asked off the main thread."""

    risk_id: int
    article: str
    user: str


def _prompt_user(
    project: Project, risk: Risk, source: Source | None, article: str, prompt_name: str
) -> str:
    from tracker.prompts import load_prompt

    return load_prompt(prompt_name).render_user(**build_context(project, risk, source, article))


def ask(question: Question, *, extractor, prompt_name: str = "risk-confirm-v1") -> Judgement:
    """The one model call. Touches no database: everything it needs is in `question`."""
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    try:
        reply = extractor.complete(
            system=load_prompt(prompt_name).system, user=question.user, max_tokens=MAX_TOKENS
        )
    except LLMError as exc:
        log.warning("risk confirmation failed for risk %s: %s", question.risk_id, exc)
        return Judgement(question.risk_id, "error", reason=str(exc)[:160])

    try:
        payload = parse_json_object(reply.text)
    except (LLMJsonError, ValueError):
        return Judgement(question.risk_id, "error", reason="unusable reply")

    verdict = str(payload.get("verdict") or "").strip().lower()
    quote = str(payload.get("quote") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if verdict not in {"confirmed", "refuted", "unclear"}:
        verdict = "unclear"
    return Judgement(question.risk_id, verdict, quote=quote, reason=reason, confidence=confidence)


def verify_quote(quote: str, article: str, category: str) -> tuple[str, str]:
    """Return `(accepted quote, refusal reason)`. Exactly one is non-empty.

    The same two gates the extraction path applies, in the same order and with the
    same helpers — a confirmation that used a looser test than the gate it is
    overturning would be a way to launder a paraphrase into a citation. Both read
    the article's whole sentence around the quote (`_verbatim_run` returns it), so a
    model that copies the clause naming the obstacle is judged on what the sentence
    says, exactly as the extraction is.
    """
    from tracker.ingest.crawl import _risk_quote_supports, _verbatim_run

    if not quote:
        return "", "no quote offered"
    run = _verbatim_run(quote, article)
    if not run.text:
        return "", "that sentence is not in the article"
    if not _risk_quote_supports(category, run.text):
        return "", f"{OFF_CATEGORY} a `{category}` obstacle"
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
        risk.closed_at = utcnow()
        return "refuted"
    return "unclear"


def run(
    session: Session,
    risks: list[Risk],
    *,
    extractor,
    limit: int | None = None,
    again: bool = False,
    cache_dir: Path | None = None,
    apply: bool = True,
    workers: int = 1,
    on_each=None,
    on_start=None,
):
    """Read each obstacle's article and settle it, through `judged.run`. Returns its `Run`.

    One model call per obstacle, up to `limit` of them, after holding back those
    already judged on the same article (`fresh`). Each answer is committed as it
    lands, so a run killed part way keeps every answer before it; one obstacle that
    fails is rolled back and the rest go on.

    What is remembered (`tracker.declines`, kind `risk`), against a hash of the
    obstacle and its article:

    * **unclear** — the model's own answer on this article: held until the article
      or the obstacle changes, or the cooldown lapses.
    * **no_article** — nothing to read, so nothing was asked. Held the same way, and
      for the same reason: the hash covers the article text, so the day the cache
      holds the page the question is new. It used to be recorded nowhere and asked
      about again every round.
    * **error** — the call failed or the reply was unusable: held for the night only.

    Args:
        apply: False judges and writes nothing, so a run can be previewed at full
            cost but zero risk. The judgement is identical either way — the same
            code decides, and only the assignment is skipped.
        workers: calls in flight at once; one per obstacle's campus per wave.
        on_each: called with each Outcome as it lands, for progress output.
    """
    from tracker import declines, judged

    sources = sources_for(session, risks)
    articles: dict[int, str] = {}

    def text(risk: Risk) -> str:
        if risk.id not in articles:
            articles[risk.id] = article_for(sources.get(risk.source_id), cache_dir=cache_dir)
        return articles[risk.id]

    def shell(risk: Risk, project: Project, judgement: Judgement) -> Outcome:
        source = sources.get(risk.source_id)
        return Outcome(
            risk_id=risk.id,
            project_id=project.id,
            project=f"{project.company} — {project.name}",
            category=risk.category,
            severity=risk.severity,
            summary=risk.summary,
            judgement=judgement,
            url=(source.url if source else ""),
        )

    def prepare(risk: Risk):
        project = session.get(Project, risk.project_id)
        if project is None:
            return judged.Done(result=None)
        article = text(risk)
        if not article.strip():
            outcome = shell(
                risk,
                project,
                Judgement(risk.id, "error", reason="no article text is available to read"),
            )
            outcome.result = "no_article"
            return judged.Done(outcome, decline="no_article", reason=outcome.judgement.reason)
        source = sources.get(risk.source_id)
        return Question(risk.id, article, _prompt_user(project, risk, source, article, PROMPT))

    def settle(risk: Risk, question: Question, judgement: Judgement) -> judged.Done:
        outcome = shell(risk, session.get(Project, risk.project_id), judgement)
        if not apply:
            # Judge exactly as `apply` would, against a throwaway copy, so a
            # preview cannot report an outcome the real run would not produce.
            probe = _Detached(risk.category, risk.quote, risk.unconfirmed, risk.status)
            outcome.result = apply_judgement(probe, judgement, question.article)  # type: ignore[arg-type]
            return judged.Done(outcome)
        outcome.result = apply_judgement(risk, judgement, question.article)
        if outcome.result == "error":
            # A failed call, not an answer: held for the rest of the night only.
            return judged.Done(outcome, decline=declines.ERROR, reason=judgement.reason)
        if outcome.result == "unclear":
            return judged.Done(outcome, decline="unclear", reason=judgement.reason)
        return judged.Done(outcome, forget=True)

    def failed(risk: Risk, error: BaseException) -> Outcome | None:
        project = session.get(Project, risk.project_id)
        if project is None:
            return None
        outcome = shell(risk, project, Judgement(risk.id, "error", reason=str(error)[:160]))
        outcome.result = "error"
        return outcome

    return judged.run(
        session,
        risks,
        kind="risk",
        subject=lambda r: str(r.id),
        fingerprint=lambda r: evidence(r, sources.get(r.source_id), article=text(r)),
        projects=lambda r: [r.project_id],
        prepare=prepare,
        ask=lambda question, _db: ask(question, extractor=extractor),
        apply=settle,
        failed=failed,
        limit=limit,
        again=again,
        # Lazy: the fingerprint is the whole article, and nothing past the limit
        # needs reading. A held-back obstacle is still skipped before the limit.
        lazy=True,
        known=held_declines(session),
        workers=workers,
        commit=apply,
        on_each=on_each,
        on_start=on_start,
        label="risks",
    )


def confirm(
    session: Session,
    risks: list[Risk],
    *,
    extractor,
    cache_dir: Path | None = None,
    apply: bool = True,
    on_each=None,
) -> list[Outcome]:
    """Settle exactly these obstacles, none held back: `run` without the selection."""
    return run(
        session,
        risks,
        extractor=extractor,
        again=True,
        cache_dir=cache_dir,
        apply=apply,
        on_each=on_each,
    ).results


#: The prompt `run` asks with.
PROMPT: Final = "risk-confirm-v1"


@dataclass
class _Detached:
    """A stand-in risk for `--dry-run`, with the four attributes the write touches."""

    category: str
    quote: str | None
    unconfirmed: str | None
    status: str


__all__ = [
    "ARTICLE_BUDGET",
    "CATEGORY_CHECK_REVISED_AT",
    "MIN_CONFIDENCE",
    "OFF_CATEGORY",
    "PROMPT",
    "REFUTED_STATUS",
    "Judgement",
    "Outcome",
    "Question",
    "apply_judgement",
    "article_for",
    "ask",
    "build_context",
    "confirm",
    "evidence",
    "fresh",
    "held_declines",
    "judge",
    "run",
    "sources_for",
    "unconfirmed_risks",
    "verify_quote",
]
