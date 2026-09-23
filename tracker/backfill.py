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
    #: Rows where the article was read and named no tranche for this campus. Counted
    #: because it is a *result*, not a failure: most campuses really are one block,
    #: and recording the read is what lets the run converge.
    read_no_blocks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    notes: list[str] = field(default_factory=list)

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("articles selected", self.urls),
            ("read", self.read),
            ("blocks written", self.blocks_written),
            ("projects touched", self.projects_touched),
            ("read, one undivided campus", self.read_no_blocks),
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
    from tracker.ingest.crawl import extract_one, vague_block_note
    from tracker.ingest.fetch import FetchResult, HttpxFetcher, cache_path
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

        # `extract_one` rather than a bare `complete`, for its corrective retry.
        # Measured on the first live tranche: 5 of 25 articles failed to parse, every
        # one of them a reply that spent its whole budget inside `<think>` and never
        # reached the JSON. A verbatim retry reproduces that ramble, so the crawl path
        # already learned to send a *different* request with a doubled budget — 20% of
        # the articles were being thrown away for want of reusing it.
        outcome = extract_one(
            FetchResult(url=pick.url, ok=True, markdown=text),
            prompt=prompt,
            extractor=extractor,
            settings=settings,
        )
        report.prompt_tokens += outcome.prompt_tokens
        report.completion_tokens += outcome.completion_tokens
        report.read += 1

        if outcome.status == "llm_error":
            report.fetch_error += 1
            log.warning("%s: %s", pick.url, outcome.error)
            continue
        if outcome.status not in ("ok", "no_project"):
            report.parse_error += 1
            log.warning("%s: %s", pick.url, outcome.error)
            continue
        extracted = list(outcome.records)
        if not extracted:
            continue

        # Matched on the record's *normalized* fields rather than the model's raw
        # reply, so the pairing sees the same "Fort Worth" the row was stored with,
        # and the pairing logic stays a pure function of plain dicts.
        claims = [record.project for record in extracted]
        siblings = [p for p in (session.get(Project, pid) for pid in pick.project_ids) if p]
        for project in siblings:
            pid = project.id
            raw = _match(claims, project, sole_candidate=len(pick.project_ids) == 1)
            if raw is None:
                report.notes.append(
                    f"#{pid}: {pick.url} describes no project matching this row; left alone"
                )
                # Still a recorded read. The article was fetched and understood; it
                # simply is not about this campus, and that is a fact worth storing
                # so the same call is never paid for twice.
                row = next((s for s in project.sources if s.url == pick.url), None)
                if row is not None and not dry_run and not row.blocks:
                    row.blocks = "[]"
                    report.read_no_blocks += 1
                    session.commit()
                continue
            record = extracted[next(i for i, c in enumerate(claims) if c is raw)]
            found = [block for source in record.sources for block in source.blocks]
            found, elsewhere = _route(found, project, siblings)
            if elsewhere:
                report.notes.append(
                    f"#{pid}: {len(elsewhere)} block(s) in this article name another of "
                    f"the operator's sites ({', '.join(b.label for b in elsewhere)}); "
                    "left off this row"
                )
            # The record described every block in the article; this row may have kept
            # only some, so the 待确认 disclosure is built from what it actually kept.
            block_notes = vague_block_note(found)

            row = next((s for s in project.sources if s.url == pick.url), None)
            if row is None:
                continue
            if dry_run:
                report.blocks_written += len(found)
                if found:
                    touched.add(pid)
                continue

            # **An empty read is recorded, not skipped.** Leaving the column NULL made
            # "read it, this campus is one undivided thing" indistinguishable from
            # "never read it" — so `candidates` re-offered the same articles forever
            # and the run could never reach "nothing to do". Measured across four
            # tranches: 391 reads over 229 distinct URLs, about 40% of the spend on
            # articles already read. It also made 88 bare rows look like a lapse when
            # most of those campuses really are one block.
            row.blocks = json.dumps(
                [b.as_json() for b in sorted(found, key=lambda b: b.label.lower())],
                ensure_ascii=False,
            )
            if not found:
                report.read_no_blocks += 1
                session.commit()
                continue
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


# --- re-gating the claim envelope ------------------------------------------


@dataclass
class ScopeReport:
    """What a re-gate of the stored claim envelopes changed."""

    sources: int = 0
    claims: int = 0
    changed: int = 0
    #: old scope -> new scope -> count, for the only summary that matters.
    moved: dict[str, dict[str, int]] = field(default_factory=dict)

    def note(self, old: str, new: str) -> None:
        bucket = "block:*" if new.startswith("block:") else new
        was = "block:*" if old.startswith("block:") else old
        self.moved.setdefault(was, {}).setdefault(bucket, 0)
        self.moved[was][bucket] += 1


def regate_scope(session: Session, *, apply: bool = False) -> ScopeReport:
    """Re-run `axis_gate` over every stored claim envelope. No LLM, no network.

    **Why this is free, which is the whole reason it exists.** `axis_gate` is a pure
    function of the entry, the stored quote and the record's own labels — and all
    three are already in the database. So the labels can be recomputed the way
    `backfill derive` recomputes derived values, without re-reading a single
    article. An agent re-scoping the same claims would cost ~77,000 tokens a row.

    Only `this_site` is re-gated. Every other value was licensed by wording or by
    resolving against a block when it was written, and the gate has not changed for
    those; `this_site` is the one that used to be unrefusable, so it is the only one
    whose stored value carries no information about whether it was checked.

    Site identity comes from the *project* rather than from the arriving record,
    which is the one way this differs from the ingest path — and it is the right
    source here: the row's name, city and county are what a stored claim is a claim
    about, and 92.9% of sources state at least one of them anyway.
    """
    import json

    from tracker.ingest.crawl import axis_gate, site_names

    report = ScopeReport()
    for project in session.scalars(select(Project)).all():
        labels = frozenset(
            (b.label or "").strip().lower()
            for b in (project.blocks or ())
            if (b.label or "").strip()
        )
        names = site_names({"name": project.name, "city": project.city, "county": project.county})
        for source in project.sources:
            if not source.claim_meta:
                continue
            try:
                meta = json.loads(source.claim_meta)
                quotes = json.loads(source.quotes or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(meta, dict) or not isinstance(quotes, dict):
                continue
            report.sources += 1
            touched = False
            for name, entry in meta.items():
                if not isinstance(entry, dict) or entry.get("scope") != "this_site":
                    continue
                report.claims += 1
                got = axis_gate(
                    {"scope": "this_site"},
                    quotes.get(name) or "",
                    block_labels=labels,
                    site_names=names,
                )["scope"]
                if got == "this_site":
                    continue
                report.changed += 1
                report.note("this_site", got)
                entry["scope"] = got
                touched = True
            if touched and apply:
                source.claim_meta = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    if apply:
        session.flush()
    return report


# --- deriving the basis axis ------------------------------------------------


@dataclass
class BasisReport:
    """What deriving the `basis` axis found in the quotes already on disk."""

    sources: int = 0
    #: Capacity claims examined — only `mw_planned` and `mw_built` carry a basis.
    claims: int = 0
    #: Claims that gained a basis they did not have.
    changed: int = 0
    #: basis -> how many claims ended up there, **including the default**, which
    #: is counted here and deliberately not stored. `unspecified` dominating is
    #: the finding rather than a failure: it is the share of the database whose
    #: article never said which kind of megawatt it meant. Counting it here is how
    #: that share stays visible without an envelope on every capacity claim — see
    #: `derive_basis` for why storing it would break the coverage measurement.
    found: dict[str, int] = field(default_factory=dict)
    #: Projects whose cached `mw_*_basis` moved.
    projects_touched: int = 0

    def note(self, basis: str) -> None:
        self.found[basis] = self.found.get(basis, 0) + 1

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("sources with an envelope", self.sources),
            ("capacity claims read", self.claims),
            ("claims given a basis", self.changed),
            ("projects whose cache moved", self.projects_touched),
        ]


@dataclass
class PrecisionReport:
    """What reading date precision out of the stored quotes found."""

    #: Quoted date claims examined.
    claims: int = 0
    #: Claims whose recorded precision moved.
    changed: int = 0
    #: precision -> claims that ended there; `day` includes every date the quote
    #: does not narrow, which is stored as no precision at all.
    found: dict[str, int] = field(default_factory=dict)
    #: Projects whose cached `*_precision` columns moved.
    projects_touched: int = 0

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("quoted date claims read", self.claims),
            ("claims given a precision", self.changed),
            ("projects whose cache moved", self.projects_touched),
        ]


def derive_date_precision(session: Session, *, apply: bool = False) -> PrecisionReport:
    """Read how precisely each dated claim's own sentence states it. No LLM, no network.

    The extraction prompt asks for ISO dates and has the model write a bare year as
    `YYYY-01-01`, so the parser recorded a day for "online in 2027" and the row
    showed 1 January. Four of 1,455 date claims carried a precision on the snapshot
    this was written for. The quote beside each date is already on disk, so this is
    the same free re-read `derive_basis` performs: `normalize.precision_in_quote`
    looks only at the words before the year, and a date the sentence does not narrow
    keeps no precision rather than a guessed one.

    A precision the *parser* recorded is never overwritten — it saw the model's own
    date string, and "Q3 2025" parsed as a quarter is already the right answer.
    """
    import json

    from tracker.normalize import precision_in_quote
    from tracker.upsert import apply_date_precision, claims_by_field

    report = PrecisionReport()
    for project in session.scalars(select(Project)).all():
        touched_project = False
        for source in project.sources:
            if not source.quotes:
                continue
            try:
                quotes = json.loads(source.quotes or "{}")
                claims = json.loads(source.claims or "{}")
                meta = json.loads(source.claim_meta or "{}")
            except (TypeError, ValueError):
                continue
            if not all(isinstance(x, dict) for x in (quotes, claims, meta)):
                continue
            touched = False
            for name in ("first_announced", "expected_online"):
                quote, value = quotes.get(name), claims.get(name)
                if not quote or not value:
                    continue
                report.claims += 1
                entry = meta.get(name) if isinstance(meta.get(name), dict) else {}
                if entry.get("date_precision"):
                    report.found[entry["date_precision"]] = (
                        report.found.get(entry["date_precision"], 0) + 1
                    )
                    continue
                stated = precision_in_quote(value, quote)
                report.found[stated or "day"] = report.found.get(stated or "day", 0) + 1
                if not stated:
                    continue
                report.changed += 1
                meta[name] = {**entry, "date_precision": stated}
                touched = True
            if touched and apply:
                source.claim_meta = json.dumps(meta, sort_keys=True, ensure_ascii=False)
                touched_project = True
        if touched_project and apply:
            before = (project.first_announced_precision, project.expected_online_precision)
            apply_date_precision(project, claims_by_field(list(project.sources)))
            if (project.first_announced_precision, project.expected_online_precision) != before:
                report.projects_touched += 1
    if apply:
        session.flush()
    return report


def derive_basis(session: Session, *, apply: bool = False) -> BasisReport:
    """Fill the `basis` axis from the quotes already stored. No LLM, no network.

    **Why this one is backfillable when 0015's axes were not.** That migration
    said its axes "cannot" be backfilled and was right about `bound`, `modality`
    and `as_of`: each is a fact about how an article was worded that only a
    re-read can recover. This axis is different in exactly one way that matters —
    it is derived from the *stored quote*, and the stored quote is on disk. So
    reading it is the same free operation `regate_scope` performs, not an
    inference about an article nobody re-opened.

    Nothing is guessed from the number. A figure whose sentence says nothing about
    IT load, gross power or nameplate stays `unspecified`, and the share of the
    database in that position is the measurement this axis was added to make.
    Inferring a basis from a plausible-looking `$/MW` ratio would manufacture a
    qualifier no publisher wrote, which is the line `0015` drew and this keeps.
    """
    import json

    from tracker.ingest.crawl import axis_gate
    from tracker.upsert import apply_mw_basis, claims_by_field
    from tracker.vocab import BASIS_FIELDS, CLAIM_AXIS_DEFAULTS

    report = BasisReport()
    for project in session.scalars(select(Project)).all():
        touched_project = False
        for source in project.sources:
            if not source.claim_meta and not source.quotes:
                continue
            try:
                meta = json.loads(source.claim_meta or "{}")
                quotes = json.loads(source.quotes or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(meta, dict) or not isinstance(quotes, dict):
                continue
            try:
                claims = json.loads(source.claims or "{}")
            except (TypeError, ValueError):
                claims = {}
            if not isinstance(claims, dict):
                claims = {}
            report.sources += 1
            touched = False
            for name in sorted(BASIS_FIELDS):
                quote = quotes.get(name)
                if not quote:
                    continue
                report.claims += 1
                # This source's own figure, not the project's. The axis is
                # positional now, so it has to be anchored on the number *this*
                # sentence states — and a source that lost the merge states a
                # different one, which would put the anchor on a figure its quote
                # does not contain and read `unspecified` off every disagreement.
                got = axis_gate({}, quote, field=name, value=claims.get(name))["basis"]
                report.note(got)
                entry = meta.get(name)
                if not isinstance(entry, dict):
                    entry = {}
                # A default basis is counted and not stored, matching what the
                # ingest path does and for the same reason: `unspecified` is the
                # answer for most capacity figures, so writing it would attach an
                # envelope to nearly every one and make the measured coverage of
                # every *other* axis look near-total. See `crawl._claim_axes`.
                #
                # `pop` rather than skip, so a run after this rule changed clears
                # the defaults an earlier one wrote instead of leaving the two
                # paths permanently disagreeing about the same claim.
                wanted = None if got == CLAIM_AXIS_DEFAULTS["basis"] else got
                if entry.get("basis") == wanted:
                    continue
                report.changed += 1
                if wanted is None:
                    entry.pop("basis", None)
                    # An entry emptied of every axis is an envelope with nothing
                    # in it, which is what the neutrality rule refuses.
                    if entry:
                        meta[name] = entry
                    else:
                        meta.pop(name, None)
                else:
                    entry["basis"] = wanted
                    meta[name] = entry
                touched = True
            if touched and apply:
                source.claim_meta = json.dumps(meta, sort_keys=True, ensure_ascii=False)
                touched_project = True
        if touched_project and apply:
            # The cached column is a function of the winning claim, so it has to
            # be recomputed after the envelope moves rather than guessed from the
            # last claim read — `apply_mw_basis` asks the write path's own merge
            # order, which is the discipline `gaps.provenance` exists to keep.
            before = (project.mw_planned_basis, project.mw_built_basis)
            apply_mw_basis(project, claims_by_field(list(project.sources)))
            if (project.mw_planned_basis, project.mw_built_basis) != before:
                report.projects_touched += 1
    if apply:
        session.flush()
    return report


# --- one article, one citation, one queue state ---------------------------


@dataclass
class UrlReport:
    """What `backfill urls` found, and with `--apply` repaired."""

    #: (project id, the citation kept, the other spellings folded into it)
    folded: list[tuple[int, str, list[str]]] = field(default_factory=list)
    #: Claims a kept citation took from a copy folded into it.
    claims_carried: int = 0
    #: (url, the failed status it held) for queue rows of articles already read.
    restored: list[tuple[str, str]] = field(default_factory=list)

    def as_rows(self) -> list[tuple[str, int]]:
        return [
            ("rows citing one article more than once", len(self.folded)),
            ("extra citations folded away", sum(len(f) for _, _, f in self.folded)),
            ("claims carried from a folded copy", self.claims_carried),
            ("read articles taken out of the retry pool", len(self.restored)),
        ]


def repair_urls(session: Session, *, apply: bool = False) -> UrlReport:
    """Fold a row's second citation of one article, and unqueue articles already read.

    Both are what the ingest path did before it compared URLs by identity, and it
    now prevents both — but neither prevention reaches rows already stored:

    * **One article cited twice by one row**, under two spellings that
      `normalize.url_identity` says are the same page — a trailing slash, `www.`,
      the scheme, or Google's `srsltid`, which gives every search result its own
      URL. On the snapshot this was written for, 17 rows held 23 extra citations
      that way, one of them the same market report five times. Each extra copy is
      a second vote from one article, and where its reading differs it makes a
      field look contested. The earliest copy is kept and absorbs the others the
      way `tracker merge` folds a shared citation (`upsert.fold_source`): its own
      values stand, the rival figure is named in the notes, and milestones and
      obstacles move to it. The URLs themselves are not rewritten.
    * **An article that was read, queued for a retry.** A failed re-read used to
      overwrite the URL's `ok` with the failure, which put a cited article in the
      pool `--retry-failed` and enrich's retry harvester spend a try on every run
      (36 on the snapshot). A citation made by reading the article is proof it was
      read, so the queue row goes back to `ok`; the failure stays recorded beside
      it, as a failed re-read is now recorded.

    Free: no model, no network. Safe to re-run — a second pass finds nothing.
    """
    from tracker.confidence import PLACEHOLDER_MARKER
    from tracker.ingest.discover import RETRYABLE_STATUSES
    from tracker.merge import _repoint
    from tracker.models import IngestUrl
    from tracker.normalize import url_identity
    from tracker.upsert import (
        SOURCE_NOTE_PREFIX,
        fold_source,
        recompute_from_sources,
        record_tag,
    )

    report = UrlReport()
    read: set[str] = set()
    for project in session.scalars(select(Project).order_by(Project.id)).all():
        copies: dict[str, list[Source]] = {}
        for source in project.sources:
            copies.setdefault(url_identity(source.url), []).append(source)
            if _was_read(source, placeholder=PLACEHOLDER_MARKER):
                read.add(url_identity(source.url))
        twice = [group for group in copies.values() if len(group) > 1]
        if not twice:
            continue
        rivals: list[str] = []
        for group in twice:
            keep, *others = sorted(group, key=lambda s: (s.fetched_at, s.id))
            report.folded.append((project.id, keep.url, [s.url for s in others]))
            if not apply:
                continue
            marker = f"{SOURCE_NOTE_PREFIX}[{record_tag([keep.url])}]"
            for other in others:
                lines, taken = fold_source(
                    keep, other, given_by=f"the copy of this citation stored as {other.url}"
                )
                rivals += [f"{marker} {line}" for line in lines]
                report.claims_carried += taken
                _repoint(session, other.id, keep.id)
                project.sources.remove(other)
                session.delete(other)
        if not apply:
            continue
        session.flush()
        if rivals:
            # Before the recompute, which keeps every contributed line it did not
            # write; tagged with the kept citation, so its next reading replaces them.
            lines = [line for line in (project.notes or "").splitlines() if line.strip()]
            project.notes = "\n".join(
                [*lines, *(r for r in dict.fromkeys(rivals) if r not in lines)]
            )
        recompute_from_sources(session, project)

    for row in session.scalars(
        select(IngestUrl).where(IngestUrl.status.in_(RETRYABLE_STATUSES)).order_by(IngestUrl.id)
    ).all():
        if url_identity(row.url) not in read:
            continue
        report.restored.append((row.url, row.status))
        if apply:
            row.status = "ok"
    if apply:
        session.flush()
    return report


def _was_read(source: Source, *, placeholder: str) -> bool:
    """A citation made by reading the article, as `crawl.stale_sources` counts one.

    Not a placeholder, not an ISO queue row, and not `derived:` or `inferred:`,
    which were computed from reference data rather than read.
    """
    extractor = source.extractor or ""
    return (
        placeholder not in (source.url or "")
        and source.source_type != "iso_queue"
        and not extractor.startswith(("derived:", "inferred:"))
    )
