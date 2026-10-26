"""A model reads a queued page's headline and sets aside what cannot be campus news.

The nightly crawl reads 900 queued pages and about half find nothing: of the 4,642
reads with a headline settled by 2026-10-26, 2,235 found no campus. Some of those
headlines already said so — a hire in Madrid, an award, a webinar, a VPS price list,
an AI-bubble column, Microsoft walking away from a UK site — and the address rules in
:mod:`tracker.ingest.screen` cannot tell them, because what gives them away is the
meaning of the headline, not its form. Each costs a full extraction call to learn
what the headline said.

Here a cheap call with no reasoning reads fifty headlines at once and names the ones
that are plainly one of a few kinds (:data:`KINDS`); everything else is read as before.
A headline costs a few dozen tokens to judge and an article several thousand to read,
so the screen pays for itself if it sets aside one page in a few hundred.

**What it must not do is lose a campus**, so it is measured before it is trusted:
`tracker queue screen --measure` puts the past reads' headlines through it and
counts how many reads that found a campus it would have set aside, listing each. It
is off in the crawl until switched on (`ingest crawl --from-queue --screen`), and the
nightly loop switches it on only with `--screen`.

A page set aside is `skipped` with :data:`ASIDE` and the kind in front, not deleted,
so `tracker queue` shows why. Unlike the address rules it is not re-asked on every
run — a model's verdict is a call, not a free test — so a page it kept is stamped
with :data:`STAMP` and not asked again under this prompt, and `tracker queue screen
--undo` puts back everything it set aside and keeps those pages from being set
aside again.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from tracker.llm import LLMError, parse_json_object, spend_stage

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from tracker.llm import Extractor
    from tracker.models import IngestUrl

log = logging.getLogger(__name__)

#: Stamped on a queued page the screen kept. Change it when the prompt changes, and
#: every page is asked again under the new one.
STAMP: Final = "headline-1"

#: The `error` a page set aside by its headline carries; the kind follows it. A
#: different prefix from `screen.SET_ASIDE`, whose pages are re-asked and put back on
#: every run by a free test this screen does not have.
ASIDE: Final = "set aside by its headline: "

#: Headlines per call. Fifty fit in a few thousand tokens with the instructions.
BATCH: Final = 50

#: What a headline has to plainly be for its page to be set aside, and how the
#: prompt describes each. Anything else is read.
KINDS: Final[dict[str, str]] = {
    "abroad": "about a place outside the United States, and the line names no US place",
    "people": (
        "a hire, appointment, promotion or award, a company's or a person's ranking, "
        "a profile or an obituary"
    ),
    "event": "a conference, webinar, trade show, podcast or other event listing",
    "markets": "stock, bond, earnings or market commentary that names no site",
    "product": (
        "a cloud, hosting, GPU, network or software product or partnership that names no new site"
    ),
    "unrelated": "nothing to do with data centers, or the power, land, water or permits they need",
}

SYSTEM: Final = (
    """You screen the reading queue of a tracker of data-center campuses in the United
States: sites proposed, rezoned, permitted, under construction, energised, financed,
sold, leased, delayed, cancelled or running. Reading an article costs money. Your job
is to name the lines whose article certainly cannot report anything about a particular
US data-center campus, so they are not read.

Each line is a headline, then the page's address after "|". Set a line aside only when
it is plainly one of these:

"""
    + "\n".join(f"{kind}: {text}" for kind, text in KINDS.items())
    + """

Keep every other line, and keep a line whenever you are unsure. Keep a line that names
a US state, county, city or campus. Keep financings, sales, acquisitions, leases,
lawsuits, moratoriums, zoning and tax decisions, utility and power deals, and articles
about the industry in general or in one state: those often name a campus in the body.

Answer with JSON only, listing just the lines to set aside, each with one of the reason
words above:
{"aside": {"<line number>": "<reason>"}}
{"aside": {}} keeps them all."""
)


@dataclass(frozen=True)
class Line:
    """One headline to judge: `key` is the caller's, returned with the verdict."""

    key: int
    title: str
    url: str


def _where(url: str) -> str:
    """Host and path, shortened: a slug often says where a story is."""
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    return f"{host}{parts.path}"[:100]


def _ask(extractor: Extractor, chunk: Sequence[Line]) -> dict[int, str]:
    """{key: kind} for the lines of one call to set aside.

    **A call that fails, or answers in a shape not asked for, keeps every line**: a
    screen that cannot answer must cost a read, never a campus.
    """
    user = "\n".join(
        f"{number}. {' '.join(line.title.split())[:200]} | {_where(line.url)}"
        for number, line in enumerate(chunk, 1)
    )
    try:
        reply = extractor.complete(system=SYSTEM, user=user, max_tokens=2048)
        payload = parse_json_object(reply.text)
    except (LLMError, ValueError) as exc:
        log.warning("headline screen: a batch of %d kept unjudged: %s", len(chunk), exc)
        return {}
    aside = payload.get("aside") if isinstance(payload, dict) else None
    if not isinstance(aside, dict):
        log.warning("headline screen: a batch of %d kept, the answer had no 'aside'", len(chunk))
        return {}
    verdicts: dict[int, str] = {}
    for number, kind in aside.items():
        try:
            index = int(str(number).strip().rstrip(".")) - 1
        except ValueError:
            continue
        kind = str(kind).strip().lower()
        if 0 <= index < len(chunk) and kind in KINDS:
            verdicts[chunk[index].key] = kind
    return verdicts


def judge(extractor: Extractor, lines: Sequence[Line], *, batch: int = BATCH) -> dict[int, str]:
    """{key: kind} for every line to set aside; a line not in it is kept.

    Raises `LLMUnavailable` when the provider cannot answer at all (no key, peak
    hours): the caller decides whether to read unscreened.
    """
    verdicts: dict[int, str] = {}
    with spend_stage("headline screen"):
        for start in range(0, len(lines), batch):
            verdicts.update(_ask(extractor, lines[start : start + batch]))
    return verdicts


@dataclass
class Screened:
    """What one pass over the queue decided."""

    kept: list[IngestUrl] = field(default_factory=list)
    aside: list[tuple[IngestUrl, str]] = field(default_factory=list)
    #: Headlines sent to the model; the rest were kept unasked.
    asked: int = 0


def _judged_before(row: IngestUrl) -> bool:
    return row.screened_by == STAMP


def screen_queue(
    extractor: Extractor,
    rows: Iterable[IngestUrl],
    *,
    want: int | None,
    exempt: Collection[int] = (),
    apply: bool = True,
    batch: int = BATCH,
) -> Screened:
    """Walk queued pages in reading order and keep the first `want` the screen keeps.

    A page is kept without asking when it has no headline, was kept under this
    prompt before, or is in `exempt` — the go-live results the delay flags wait on
    (`discover.deciding`), whose headlines are a search's and are read whatever they
    say. The rest are asked in batches, never many more than `want` still needs, so a
    crawl of 900 asks about the pages it would read and the few that replace the
    ones set aside. With `apply`, a kept page is stamped and a page set aside is
    `skipped` with :data:`ASIDE` and its kind; the caller commits.
    """
    decided: dict[int, str | None] = {}
    order: list[IngestUrl] = []
    waiting: list[IngestUrl] = []
    asked = kept = 0

    def flush() -> None:
        nonlocal asked, kept
        verdicts = judge(
            extractor, [Line(r.id, r.title or "", r.url) for r in waiting], batch=batch
        )
        asked += len(waiting)
        for row in waiting:
            decided[row.id] = verdicts.get(row.id)
            kept += row.id not in verdicts
        waiting.clear()

    for row in rows:
        if want is not None and kept >= want:
            break
        order.append(row)
        if row.id in exempt or not (row.title or "").strip() or _judged_before(row):
            decided[row.id] = None
            kept += 1
            continue
        waiting.append(row)
        if len(waiting) >= batch or (want is not None and kept + len(waiting) >= want):
            flush()
    if waiting:
        flush()

    result = Screened(asked=asked)
    for row in order:
        kind = decided.get(row.id)
        if kind is None:
            if want is None or len(result.kept) < want:
                result.kept.append(row)
            if apply and row.id not in exempt and (row.title or "").strip():
                row.screened_by = STAMP
        else:
            result.aside.append((row, kind))
            if apply:
                row.status, row.error = "skipped", f"{ASIDE}{kind}"
    return result


def summary(kinds: Iterable[str]) -> str:
    """'12 abroad, 7 people, …' for a run's one-line report."""
    from collections import Counter

    return ", ".join(f"{count} {kind}" for kind, count in Counter(kinds).most_common())


@dataclass
class Measured:
    """The screen's verdicts on pages already read, against what reading them found."""

    #: Past reads judged that found a campus, and that found none.
    found: int = 0
    empty: int = 0
    #: Reads that found a campus the screen would have set aside: the page, the kind,
    #: and whether a campus cites the page today. Every one is a loss.
    lost: list[tuple[IngestUrl, str, bool]] = field(default_factory=list)
    #: Reads that found nothing the screen would have set aside, with the kind.
    spared: list[tuple[IngestUrl, str]] = field(default_factory=list)


def measure(session: Session, extractor: Extractor, *, limit: int | None = None) -> Measured:
    """Judge the headlines of pages already read, newest read first, and score them.

    A read that found a campus (`ok`) set aside is a campus the screen would have
    lost; one that found nothing (`no_project`) set aside is a call saved. Writes
    nothing, but each batch is a paid call.
    """
    from sqlalchemy import select

    from tracker.models import IngestUrl, Source

    query = (
        select(IngestUrl)
        .where(IngestUrl.status.in_(("ok", "no_project")))
        .where(IngestUrl.title.is_not(None), IngestUrl.title != "")
        .order_by(IngestUrl.last_tried_at.desc(), IngestUrl.id.desc())
    )
    if limit:
        query = query.limit(limit)
    rows = list(session.scalars(query))
    verdicts = judge(extractor, [Line(r.id, r.title or "", r.url) for r in rows])

    lost_urls = [r.url for r in rows if r.status == "ok" and r.id in verdicts]
    cited = (
        set(session.scalars(select(Source.url).where(Source.url.in_(lost_urls))))
        if lost_urls
        else set()
    )
    result = Measured()
    for row in rows:
        kind = verdicts.get(row.id)
        if row.status == "ok":
            result.found += 1
            if kind is not None:
                result.lost.append((row, kind, row.url in cited))
        else:
            result.empty += 1
            if kind is not None:
                result.spared.append((row, kind))
    return result


__all__ = [
    "ASIDE",
    "BATCH",
    "KINDS",
    "STAMP",
    "SYSTEM",
    "Line",
    "Measured",
    "Screened",
    "judge",
    "measure",
    "screen_queue",
    "summary",
]
