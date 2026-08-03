"""Re-read stored articles for one thing only: their capacity blocks.

Migration 0009 added the block table but wrote no rows, because turning an article
into blocks needs the article text rather than the schema. The 227 projects that
predate it therefore have no blocks until something re-reads their sources. This
is that something.

**Why this is not just `ingest crawl --force`.** A plain re-crawl re-extracts every
scalar with a model that behaves differently today than it did at ingest time. It
would churn 227 rows, move every `updated_at`, and possibly move `confidence` —
a large, unrelated change smuggled inside a backfill. So this writes exactly one
column, `source.blocks`, and then lets the ordinary rollup do its work. Everything
else on the row is left alone.

**Keyed on URL, not on source row.** 373 source rows come from a crawl but only 229
distinct URLs: 62 articles feed more than one project. Reading per row would pay
for those 62 twice.

**Resumable and safe to re-run.** A URL whose sources already carry blocks is
skipped unless `--force`, and blocks are rebuilt wholesale from `source.blocks`
keyed on `(project_id, block_key)` — so running twice writes the same rows rather
than duplicating them. That is a property of the design, not of care taken here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.models import Project, Source

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    """One article to re-read, and what it would inform."""

    url: str
    source_type: str
    project_ids: tuple[int, ...]
    cached: bool
    #: Higher reads first. See `_yield_score`.
    score: float = 0.0


@dataclass
class BackfillReport:
    urls: int = 0
    read: int = 0
    skipped_cached: int = 0
    fetch_error: int = 0
    parse_error: int = 0
    blocks_written: int = 0
    projects_touched: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    notes: list[str] = field(default_factory=list)

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("articles selected", self.urls),
            ("read", self.read),
            ("blocks written", self.blocks_written),
            ("projects touched", self.projects_touched),
            ("not cached, skipped", self.skipped_cached),
            ("fetch errors", self.fetch_error),
            ("parse errors", self.parse_error),
        ]

    @property
    def rejected(self) -> int:  # for `_print_report`
        return self.fetch_error + self.parse_error


def _yield_score(source_type: str, ids: tuple[int, ...], contested: set[int]) -> float:
    """How much block information this article is likely to carry.

    Ordered so `--limit 25` buys the most, rather than the first 25 alphabetically.

    Filings first: they publish per-phase tables, which is exactly the shape a block
    is, and the AZP-3 row that started this came from one. Then articles feeding a
    project whose sources already disagree about a name or a capacity — that
    disagreement is usually two facilities in one row, which is what blocks resolve.
    """
    score = 0.0
    if source_type == "company_filing":
        score += 3.0
    elif source_type == "trade_press":
        score += 1.0
    if any(pid in contested for pid in ids):
        score += 2.0
    # An article feeding several projects is worth more per call.
    score += 0.5 * (len(ids) - 1)
    return score


def _contested(session: Session) -> set[int]:
    """Projects whose sources disagree about the name or the capacity.

    Measured at 67 and 32 respectively on the live database. A name disagreement is
    rarely a naming dispute — it is usually two buildings sharing one row.
    """
    import json

    out: set[int] = set()
    seen: dict[int, dict[str, set]] = {}
    for pid, claims in session.execute(select(Source.project_id, Source.claims)).all():
        if not claims:
            continue
        try:
            data = json.loads(claims)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        bucket = seen.setdefault(pid, {"name": set(), "mw_planned": set()})
        for field_name in ("name", "mw_planned"):
            value = data.get(field_name)
            if value is not None:
                bucket[field_name].add(str(value).strip().lower())
    for pid, fields in seen.items():
        if len(fields["name"]) > 1 or len(fields["mw_planned"]) > 1:
            out.add(pid)
    return out


def candidates(
    session: Session,
    *,
    cache_dir: Path,
    project_id: int | None = None,
    force: bool = False,
) -> list[Candidate]:
    """Articles worth re-reading, best first.

    Only sources written by the crawl path. The Census place lookups are typed
    `government_doc` at weight 3 and would otherwise sort near the front, but there
    is no prose behind them to re-read.
    """
    from tracker.ingest.fetch import cache_path

    rows = session.execute(
        select(Source.url, Source.source_type, Source.project_id, Source.blocks, Source.extractor)
    ).all()

    grouped: dict[str, dict[str, Any]] = {}
    for url, source_type, pid, blocks, extractor in rows:
        if not (extractor or "").startswith("crawl:"):
            continue
        if project_id is not None and pid != project_id:
            continue
        entry = grouped.setdefault(url, {"source_type": source_type, "ids": [], "has_blocks": True})
        entry["ids"].append(pid)
        if not blocks:
            entry["has_blocks"] = False

    contested = _contested(session)
    out: list[Candidate] = []
    for url, entry in grouped.items():
        if entry["has_blocks"] and not force:
            continue
        ids = tuple(sorted(entry["ids"]))
        out.append(
            Candidate(
                url=url,
                source_type=entry["source_type"],
                project_ids=ids,
                cached=cache_path(url, cache_dir).exists(),
                score=_yield_score(entry["source_type"], ids, contested),
            )
        )
    # Deterministic, so a resumed run continues rather than reshuffles.
    out.sort(key=lambda c: (-c.score, c.url))
    return out


#: Overlap a candidate must reach before its blocks are written to a row.
MATCH_FLOOR = 0.5


def _match(
    extracted: list[dict[str, Any]], project: Project, *, sole_candidate: bool = False
) -> dict[str, Any] | None:
    """Which extracted project this existing row is. None when it cannot be told.

    One article routinely cites several projects, and one URL is often already cited
    by several rows, so the pairing has to be *decided* — and getting it wrong writes
    one facility's tranches onto another, which is the very failure blocks exist to
    fix.

    Two rules, both learned the hard way on real data:

    **No free pass for a single extracted project.** An earlier version returned it
    unconditionally, and a STACK Infrastructure article that yielded one project
    wrote an 80 MW "Portland Expansion" block onto STACK's San Jose, Chicago,
    Avondale, Fort Worth and New Albany campuses. Eight rows, one of them right.

    **Locality, not company.** The overlap is computed over name and city, never the
    operator — every one of those eight rows shares "STACK Infrastructure", so
    including it made the comparison meaningless exactly where it mattered.

    **A stated locality that disagrees is a veto, not a low score.** Found by the
    test below: "STACK Infrastructure"/Chicago against "STACK Infrastructure"/
    Portland still scores 0.67 on the operator's two words alone and would have
    matched. When both sides name a place and the places are different, they are
    different facilities however similar the rest reads.

    `sole_candidate` is the one exemption: when the URL is cited by a single project
    row and the article describes a single project, the original ingest already
    decided they belong together and there is nothing to confuse it with.
    """
    from tracker.point import tokens

    if sole_candidate and len(extracted) == 1:
        return extracted[0]

    where = project.city or project.county or ""
    wanted = tokens(f"{project.name} {where}")
    if not wanted:
        return None
    mine = tokens(where)

    best, best_score = None, 0.0
    for raw in extracted:
        their_where = raw.get("city") or raw.get("county") or ""
        have = tokens(f"{raw.get('name') or ''} {their_where}")
        if not have:
            continue
        theirs = tokens(their_where)
        if mine and theirs and not (mine & theirs):
            continue
        score = len(wanted & have) / len(wanted)
        if score > best_score:
            best, best_score = raw, score
    return best if best_score >= MATCH_FLOOR else None


def _distinguishing(siblings: list[Project]) -> dict[int, set[str]]:
    """What tells each of these rows apart from the others citing the same article.

    The tokens they all share are removed, because those are what make them
    indistinguishable: every Core Scientific row says "core scientific", so matching
    on it matches everything. What is left — `denton`, `dalton`, `muskogee` — is the
    only part that can route anything.
    """
    from tracker.point import tokens

    per: dict[int, set[str]] = {}
    for p in siblings:
        where = p.city or p.county or ""
        per[p.id] = set(tokens(f"{p.name} {where}"))
    if not per:
        return {}
    shared: set[str] = set.intersection(*per.values()) if len(per) > 1 else set()
    return {pid: toks - shared for pid, toks in per.items()}


def _route(found: list, project: Project, siblings: list[Project]) -> tuple[list, list]:
    """Split a portfolio article's blocks across the rows citing it.

    Returns ``(kept, dropped)`` for this row.

    An article can cover an operator's whole portfolio. A Core Scientific filing
    describes Denton, Dalton, Austin, Marble and Muskogee in one breath; the model
    returns them as one project with six blocks, and every Core Scientific row
    matches it, because `_match` can only see the company and the city and the
    company is the same. Writing all six to both rows recorded 588 MW twice.

    But the obvious fix — demand that each block's label name its row's site — is
    wrong, and measurably so: it emptied Lake Mariner, whose blocks are called
    "Akela", "La Lupa" and "HPC Leasing". A building is usually named after nothing
    in particular. That is the normal case, not a portfolio.

    So portfolio-ness is *detected* rather than assumed. If no block in the article
    names any row's distinguishing token, the article is about one site and every
    block stays. If some blocks do name rows apart, the article is a portfolio and
    then every block must earn its place: one that names no row goes nowhere, since
    it is likely a sixth campus that is not either of these two.

    Skipping is the recoverable direction — a missed block is a gap somebody can
    see, a misrouted one is a number in the wrong campus's total.
    """
    from tracker.point import tokens

    if len(siblings) < 2:
        return found, []

    distinct = _distinguishing(siblings)
    if not any(distinct.values()):
        return found, []

    named = [
        {pid for pid, toks in distinct.items() if toks & tokens(f"{b.label} {b.parent or ''}")}
        for b in found
    ]
    if not any(named):
        # Nothing here tells the rows apart. One site, ordinary blocks.
        return found, []

    kept = [b for b, rows in zip(found, named, strict=True) if project.id in rows]
    dropped = [b for b, rows in zip(found, named, strict=True) if project.id not in rows]
    return kept, dropped


def run(
    session: Session,
    picks: list[Candidate],
    *,
    extractor,
    cache_dir: Path,
    settings=None,
    refetch: bool = False,
    dry_run: bool = False,
) -> BackfillReport:
    """Re-read each article and write only its blocks."""
    import asyncio
    import json

    from tracker import blocks as blocks_mod
    from tracker.config import get_settings
    from tracker.ingest.crawl import (
        MAX_PROJECTS_PER_ARTICLE,
        _blocks,
        truncate,
    )
    from tracker.ingest.fetch import HttpxFetcher, cache_path
    from tracker.llm import LLMError, LLMJsonError, parse_json_object
    from tracker.prompts import load_prompt

    settings = settings or get_settings()
    prompt = load_prompt("extract-v1")
    report = BackfillReport(urls=len(picks))
    touched: set[int] = set()

    for pick in picks:
        path = cache_path(pick.url, cache_dir)
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
        elif refetch:
            result = asyncio.run(HttpxFetcher(settings=settings).fetch(pick.url))
            if not result.ok or not result.markdown:
                report.fetch_error += 1
                log.warning("could not re-fetch %s: %s", pick.url, result.error)
                continue
            text = result.markdown
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        else:
            report.skipped_cached += 1
            continue

        user = prompt.render_user(
            url=pick.url,
            published_date="unknown",
            markdown=truncate(text, settings.max_input_chars),
            max_projects=MAX_PROJECTS_PER_ARTICLE,
        )
        try:
            reply = extractor.complete(system=prompt.system, user=user)
        except LLMError as exc:
            report.fetch_error += 1
            log.warning("%s: %s", pick.url, exc)
            continue
        report.prompt_tokens += reply.prompt_tokens or 0
        report.completion_tokens += reply.completion_tokens or 0
        report.read += 1

        try:
            payload = parse_json_object(reply.text)
        except LLMJsonError as exc:
            report.parse_error += 1
            log.warning("%s: %s", pick.url, exc)
            continue
        extracted = [p for p in (payload.get("projects") or []) if isinstance(p, dict)]
        if not extracted:
            continue

        siblings = [p for p in (session.get(Project, pid) for pid in pick.project_ids) if p]
        for project in siblings:
            pid = project.id
            raw = _match(extracted, project, sole_candidate=len(pick.project_ids) == 1)
            if raw is None:
                report.notes.append(
                    f"#{pid}: {pick.url} describes no project matching this row; left alone"
                )
                continue
            found, block_notes = _blocks(raw, text, pick.url)
            found, elsewhere = _route(found, project, siblings)
            if elsewhere:
                report.notes.append(
                    f"#{pid}: {len(elsewhere)} block(s) in this article name another of "
                    f"the operator's sites ({', '.join(b.label for b in elsewhere)}); "
                    "left off this row"
                )
            if not found:
                continue

            row = next((s for s in project.sources if s.url == pick.url), None)
            if row is None:
                continue
            if dry_run:
                report.blocks_written += len(found)
                touched.add(pid)
                continue

            row.blocks = json.dumps(
                [b.as_json() for b in sorted(found, key=lambda b: b.label.lower())],
                ensure_ascii=False,
            )
            session.flush()
            report.blocks_written += blocks_mod.rebuild(session, project)
            blocks_mod.reconcile(project)
            report.notes.extend(f"#{pid}: {note}" for note in block_notes)
            touched.add(pid)
            # Commit per article, so a run that dies partway keeps what it read.
            session.commit()

    report.projects_touched = len(touched)
    return report


__all__ = ["BackfillReport", "Candidate", "candidates", "run"]
