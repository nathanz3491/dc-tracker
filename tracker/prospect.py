"""Go and find the operators we have no rows for.

`tracker discover` waits for a feed to mention a project. `tracker search` asks a
model to brainstorm projects and searches for them. Both are aimed at *projects*,
and both have the same blind spot: an operator absent from the model's suggestions
and from last week's headlines is absent from the database and nothing says so.
Nebius sat at zero rows through both.

This module works the other way round. `tracker.roster` says who ought to be here
and `measure` says who is not, so the input is a name — "Nebius", "Compass
Datacenters" — and the job is to turn that name into queued candidate articles.

Three lead sources, cheapest first, the same ordering `enrich` uses:

1. **queue** — URLs already in `ingest_url` naming this operator that were never
   read. Free and already ours. They need looking for on purpose: the extract phase
   is depth-first by design, so an article about an operator we have no rows for
   matches no known project and waits behind a permanent supply of better
   candidates.
2. **archive** — the configured sitemaps, filtered to URLs whose slug or title
   names the operator. Free, no API key, and it reaches back years, which matters
   because an operator we never had is usually one whose announcements are old.
3. **search** — the configured web-search backend. Four templated queries per
   operator, plus one per campus a model proposes for it.

**The model's suggestions are leads, never facts** — the same asymmetry `search.py`
rests on. Asked for an operator's US campuses it answers from training data, and
those names are only ever used to build a search query. If it invents a campus, the
search finds nothing and the run moves on; nothing it says reaches the database. A
row appears only where a real article was fetched and the evidence gate found a
verbatim quote, which is the identical path a feed-discovered article takes.

What this command does NOT do is decide it succeeded. It reports what it queued;
whether an operator actually gained a row is answered by re-measuring coverage
afterwards, which is what the CLI prints.
"""

from __future__ import annotations

import logging
import string
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from tracker.config import Settings, get_settings
from tracker.llm import Extractor, LLMError, parse_json_object
from tracker.models import utcnow
from tracker.roster import Operator, Presence

if TYPE_CHECKING:
    from tracker.ingest.discover import Candidate
    from tracker.ingest.enrich import ArchiveSweep
    from tracker.ingest.search import SearchProvider

log = logging.getLogger(__name__)

#: Search queries per operator, templates included. Deliberately small: this
#: command exists to cover many operators, not to exhaust one — `tracker point`
#: and `tracker enrich` are the depth-first tools, and they run on a project that
#: exists. Four templates plus a handful of campus names is enough to find out
#: whether the outside world has anything to say about an operator at all.
QUERIES_PER_OPERATOR = 8

#: Campuses to let the model name per operator. Each becomes one query.
CAMPUSES_PER_OPERATOR = 6


class ProspectError(RuntimeError):
    """The run cannot proceed — no search backend, or the model would not answer."""


# --- Leads ------------------------------------------------------------------


@dataclass(frozen=True)
class Lead:
    """One search query, and where the idea for it came from."""

    query: str
    #: ``template`` or ``model``. Printed, so a run says which half found what.
    origin: str


def queries_for(operator: Operator) -> list[Lead]:
    """Four searches aimed at an operator rather than at a campus.

    Templated rather than model-written, for the reason `enrich.search_queries`
    gives: the subject is already known, so a model would only paraphrase the name
    it was handed, at the cost of a call and the risk of drifting onto a competitor.

    The name is quoted so a search engine cannot helpfully substitute a synonym —
    "Nebius" unquoted returns Yandex history — and one query carries a US anchor,
    because an operator with a European fleet and one American campus otherwise
    returns four pages about the European fleet.
    """
    name = operator.name.strip()
    return [
        Lead(f'"{name}" data center megawatts', "template"),
        Lead(f'"{name}" data center campus announced', "template"),
        Lead(f'"{name}" data center construction OR permit OR interconnection', "template"),
        Lead(f'"{name}" United States data center investment', "template"),
    ]


LEADS_PROMPT_SYSTEM = """You name US data center campuses operated by one company.
You output ONLY a JSON object, no prose.

These are search LEADS, not facts. Nothing you output is stored. Every campus is
verified against a fetched article before it is recorded, so a wrong guess costs
one search and is discarded. Breadth is therefore more useful than caution: prefer
naming several plausible sites over one certain one.

Rules:
1. US sites only. A campus outside the United States is useless here, however
   large.
2. Name the site as reporting would name it — the campus, project or building
   name, or failing that the city.
3. Give the city and the two-letter state where you have them, empty strings
   where you do not. Do not guess a state to fill the field.
4. If you know of no US site for this company, return an empty list. That is a
   useful answer and is preferred over an invented one."""

LEADS_PROMPT_USER = """Company: $operator
What it is: $kind
$note
Return up to $count US data center campuses it operates, is building, or has
announced, as JSON:

{"campuses": [{"name": "<site>", "city": "<city>", "state": "<ST>"}, ...]}

Return the JSON object now."""


@dataclass(frozen=True)
class Campus:
    """A model-proposed site. Query material and nothing else."""

    name: str
    city: str = ""
    state: str = ""

    @property
    def label(self) -> str:
        where = ", ".join(part for part in (self.city, self.state) if part)
        return f"{self.name} ({where})" if where else self.name


def propose_campuses(
    extractor: Extractor, operator: Operator, *, count: int = CAMPUSES_PER_OPERATOR
) -> list[Campus]:
    """Ask the model which US sites this operator has. Returns query material.

    An empty list is a legitimate answer and is not an error: plenty of rostered
    operators have no US campus a model has heard of, and the templated queries
    above still run.
    """
    user = string.Template(LEADS_PROMPT_USER).safe_substitute(
        operator=operator.name,
        kind=operator.kind,
        note=operator.note or "",
        count=count,
    )
    try:
        reply = extractor.complete(system=LEADS_PROMPT_SYSTEM, user=user, max_tokens=1024)
    except LLMError as exc:
        raise ProspectError(f"could not propose campuses for {operator.name}: {exc}") from exc
    try:
        payload = parse_json_object(reply.text)
    except ValueError as exc:
        raise ProspectError(f"model did not return a campus list: {exc}") from exc

    raw = payload.get("campuses") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[Campus] = []
    for item in raw[:count]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        state = str(item.get("state") or "").strip().upper()[:2]
        campus = Campus(name=name, city=str(item.get("city") or "").strip(), state=state)
        if campus not in out:
            out.append(campus)
    log.info("model proposed %d campus(es) for %s", len(out), operator.name)
    return out


def campus_queries(operator: Operator, campuses: list[Campus]) -> list[Lead]:
    """One query per proposed campus, with the operator's name kept in it.

    The name stays because campus names are not unique — "Project Camellia" and
    "Hyperion" are both in use by more than one industry — and a query naming only
    the site would drag back articles about somebody else's building.
    """
    leads: list[Lead] = []
    for campus in campuses:
        where = " ".join(part for part in (campus.city, campus.state) if part)
        query = f'"{operator.name}" "{campus.name}" {where} data center megawatts'
        leads.append(Lead(" ".join(query.split()), "model"))
    return leads


# --- The key-free half: what we already saved, and the sitemaps --------------


def _haystack(url: str, title: str) -> frozenset[str]:
    """Words in a URL slug and headline, for matching an operator's name against.

    The URL is included because a newsroom slug is the most reliable place an
    operator's name survives — a headline may say "the company" by the second
    mention, but `/2025/nebius-kansas-city-expansion/` cannot.
    """
    text = f"{url} {title}".lower()
    return frozenset("".join(c if c.isalnum() else " " for c in text).split())


def archive_leads(sweep: ArchiveSweep, operator: Operator) -> list[Candidate]:
    """Swept sitemap URLs that name this operator.

    The corpus is already topic-filtered by the sweep, so this only has to decide
    whether the operator is named. Matching requires every identifying word of some
    rostered spelling — "core scientific" needs both words, so it does not collect
    every article about CoreWeave.

    A one-word operator name is the loose case and is accepted deliberately:
    "Switch" will pick up an article about a network switch, and the cost is one
    LLM call that extracts nothing. The alternative — refusing single-token names —
    would silently skip Equinix, Vantage and Crusoe.
    """
    tokens = operator.token_sets
    if not tokens:
        return []
    hits: list[Candidate] = []
    for candidate in sweep.candidates:
        words = _haystack(candidate.url, candidate.title)
        if any(spelling <= words for spelling in tokens):
            hits.append(candidate)
    return hits


def queue_leads(session: Session, operator: Operator, *, include_failed: bool = True) -> list[str]:
    """Candidates already in `ingest_url` that name this operator and were never read.

    The cheapest source there is: no search, no fetch, no key — these URLs were
    discovered at some point and are sitting in the queue.

    They need finding on purpose because the extract phase is deliberately
    depth-first: it spends each LLM call on articles covering a project already
    tracked, since a second source fills fields one article cannot. That is the
    right default and it has an edge — a queued article about an operator we have
    *no* rows for matches no known project, so it sorts last behind a permanent
    supply of better candidates and can wait indefinitely. Which is one way a
    database ends up with no Nebius row while holding a Nebius URL.

    Previously-failed URLs are included by default for the same reason `sync
    --retry-failed` exists: a host that answered 403 once is not a host that
    answers 403 forever, and the escalation ladder has grown since.
    """
    from sqlalchemy import select

    from tracker.models import IngestUrl
    from tracker.vocab import PENDING_URL_STATUS

    tokens = operator.token_sets
    if not tokens:
        return []
    from tracker.ingest.discover import RETRYABLE_STATUSES

    wanted = [PENDING_URL_STATUS, *(RETRYABLE_STATUSES if include_failed else ())]
    rows = session.scalars(select(IngestUrl).where(IngestUrl.status.in_(wanted))).all()
    return [
        row.url
        for row in rows
        if any(spelling <= _haystack(row.url, row.title or "") for spelling in tokens)
    ]


# --- Running ----------------------------------------------------------------


@dataclass
class Outcome:
    """What one operator's round found."""

    operator: Operator
    projects_before: int
    queries: list[Lead] = field(default_factory=list)
    campuses: list[Campus] = field(default_factory=list)
    hits: int = 0
    archive_hits: int = 0
    queued: list[str] = field(default_factory=list)
    #: URLs already in `ingest_url` that name this operator and were never read.
    #: Not "queued" — they were queued long ago; what they were never given is a
    #: turn, which is what this run gives them.
    from_queue: list[str] = field(default_factory=list)
    already_known: int = 0
    note: str = ""

    @property
    def name(self) -> str:
        return self.operator.name

    @property
    def to_read(self) -> list[str]:
        """Everything worth reading for this operator, cheapest first.

        The queue before the new finds, because those URLs cost nothing to obtain
        and one of them may be the article that closes the gap.
        """
        out = list(self.from_queue)
        return out + [url for url in self.queued if url not in set(out)]


@dataclass
class ProspectReport:
    outcomes: list[Outcome] = field(default_factory=list)
    queries_run: int = 0
    quota_exhausted: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)
    archive_note: str = ""

    @property
    def queued(self) -> int:
        return sum(len(o.queued) for o in self.outcomes)

    @property
    def from_queue(self) -> int:
        return sum(len(o.from_queue) for o in self.outcomes)

    @property
    def queued_urls(self) -> list[str]:
        """Every URL worth reading, operator by operator, in order.

        Ordered rather than a set: the caller crawls a prefix of it, and the
        roster's priority has to survive into which articles actually get read.
        """
        seen: set[str] = set()
        out: list[str] = []
        for outcome in self.outcomes:
            for url in outcome.to_read:
                if url not in seen:
                    seen.add(url)
                    out.append(url)
        return out


def run(
    session: Session,
    targets: list[Presence],
    *,
    provider: SearchProvider | None = None,
    extractor: Extractor | None = None,
    settings: Settings | None = None,
    sweep: ArchiveSweep | None = None,
    per_operator: int = QUERIES_PER_OPERATOR,
    dry_run: bool = False,
) -> ProspectReport:
    """Chase each target operator: mine the queue, sweep the archives, then search.

    `provider` absent means search is skipped and only the free sources are read —
    a real configuration, not a degraded one: neither the queue nor the sitemap half
    needs a key. `extractor` absent means no campus names are proposed and only the
    templated queries run, so a keyless install can still prospect.

    Queueing goes through `discover.queue_candidates`, so a URL already in
    `ingest_url` is left alone whatever state it is in, and the ordinary crawl path
    picks these up with no special casing.
    """
    from tracker.ingest import wiki
    from tracker.ingest.discover import DiscoverReport, load_config, queue_candidates
    from tracker.ingest.search import (
        QuotaExhausted,
        SearchError,
        SearchReport,
        hits_to_candidates,
    )

    settings = settings or get_settings()
    _, spec = load_config()
    report = ProspectReport()
    run_id = utcnow().strftime("prospect-%Y%m%dT%H%M%S")
    if sweep is not None:
        report.archive_note = sweep.skipped or sweep.note

    for target in targets:
        operator = target.operator
        outcome = Outcome(operator=operator, projects_before=target.projects)
        report.outcomes.append(outcome)
        candidates: list[Candidate] = []

        # 1. The queue. Free and already ours: URLs discovered at some point that
        #    name this operator and were never read, because the extract phase
        #    spends its calls depth-first on projects it already has.
        outcome.from_queue = queue_leads(session, operator)

        # 2. Archives. Also free, so it runs before anything that costs.
        if sweep is not None and not sweep.skipped:
            found = archive_leads(sweep, operator)
            outcome.archive_hits = len(found)
            candidates.extend(found)

        # 3. Search. The templated queries always; the model's campus names only
        #    when there is a model to ask.
        if provider is not None:
            leads = queries_for(operator)
            if extractor is not None:
                try:
                    outcome.campuses = propose_campuses(extractor, operator)
                except ProspectError as exc:
                    # One operator's failed brainstorm must not end the run: the
                    # templates below are the half that does not need a model.
                    report.errors.append((operator.name, str(exc)))
                    log.warning("%s", exc)
                leads += campus_queries(operator, outcome.campuses)
            leads = leads[:per_operator]
            outcome.queries = leads

            raw_hits = []
            for lead in leads:
                try:
                    hits = provider.search(lead.query, limit=settings.search_results_per_query)
                except QuotaExhausted as exc:
                    report.quota_exhausted = True
                    report.errors.append((lead.query, str(exc)))
                    log.warning("%s", exc)
                    break
                except SearchError as exc:
                    report.errors.append((lead.query, str(exc)))
                    log.warning("query %r failed: %s", lead.query, exc)
                    continue
                report.queries_run += 1
                raw_hits.extend(hits)

            shim = SearchReport()
            kept = hits_to_candidates(raw_hits, spec, report=shim)
            outcome.hits = shim.hits
            # Wikipedia's own references are primary sources, and an operator with
            # no press coverage often still has a page. Mined from the raw hits for
            # the reason `search.run` gives: the page itself can fail the keyword
            # filter while its references are exactly what we want.
            wiki_urls = [h.url for h in raw_hits if wiki.is_wikipedia(h.url)]
            if wiki_urls:
                already = {c.url for c in kept}
                kept += [
                    c for c in wiki.mine(wiki_urls, spec, settings=settings) if c.url not in already
                ]
            candidates.extend(kept)

        if not candidates:
            outcome.note = (
                f"{len(outcome.from_queue)} already queued"
                if outcome.from_queue
                else "nothing found"
            )
            if report.quota_exhausted:
                break
            continue

        # Attributed to the operator rather than to the query that found it, so
        # `tracker queue` and the source list can later answer "which of these rows
        # exist because we went looking for this company".
        label = f"prospect:{operator.name}"[:120]
        candidates = [replace(candidate, feed=label) for candidate in candidates]

        shim_queue = DiscoverReport()
        queued = queue_candidates(session, candidates, run_id=run_id, report=shim_queue)
        outcome.queued = [c.url for c in queued]
        outcome.already_known = shim_queue.already_known
        if report.quota_exhausted:
            break

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return report


__all__ = [
    "CAMPUSES_PER_OPERATOR",
    "LEADS_PROMPT_SYSTEM",
    "QUERIES_PER_OPERATOR",
    "Campus",
    "Lead",
    "Outcome",
    "ProspectError",
    "ProspectReport",
    "archive_leads",
    "campus_queries",
    "propose_campuses",
    "queries_for",
    "queue_leads",
    "run",
]
