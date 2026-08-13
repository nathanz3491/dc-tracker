"""What stage 1 costs and what it returns, per feed.

`discover` reports its own run — polled, filtered, queued — and then the number
disappears. Nothing has ever said what a feed produced *over time*: which ones
supply articles that become citations, and which spend LLM calls on articles that
turn out to describe no project at all.

That last number is the one worth surfacing. Across the live database **2,381 of
4,854 URLs that reached an LLM call produced no project — 49%**. The filter is two
tiers of keywords over a headline and a URL path, and it cannot tell an article
*about* a project from one that merely mentions the industry, so half the spend
goes on articles the extractor correctly finds nothing in. Sampled titles include
Meta newsroom posts about fact-checking and end-to-end encryption, which pass
because the feed is `topic_implied` and "expanding" is a signal term.

Reading it per feed is what makes it actionable: a `topic_implied` outlet that
covers a wider beat than the flag assumes shows up as a column of `no_project`.

Everything here is a read of `ingest_url`, which already records status, feed,
publication date and the run that found each URL. No new table, no counters to
keep in sync — the funnel is derived, so it cannot drift from what happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.models import IngestUrl, Source

#: A URL that reached the model. `discovered` has not been read yet and
#: `fetch_error` never got far enough to cost a call, so neither belongs in a
#: denominator about extraction.
REACHED_THE_MODEL: tuple[str, ...] = (
    "ok",
    "no_project",
    "thin_content",
    "parse_error",
    "llm_error",
)

#: Statuses that mean the URL is spent — read, and it will not be read again.
TERMINAL: tuple[str, ...] = ("ok", "no_project", "thin_content")


@dataclass
class FeedStat:
    """One feed's funnel: what it found, what was read, what stuck."""

    feed: str
    queued: int = 0
    #: Still waiting to be read.
    pending: int = 0
    #: Reached an LLM call.
    read: int = 0
    #: Produced at least one project.
    extracted: int = 0
    #: Reached the model and produced nothing.
    no_project: int = 0
    #: Refused before the call, as too thin to quote from.
    thin: int = 0
    #: Never fetched successfully.
    failed: int = 0
    #: Of the extracted URLs, how many back a stored citation today.
    cited: int = 0
    #: How many carry a publication date, which is what the merge tiebreak wants.
    dated: int = 0

    @property
    def waste(self) -> float:
        """Share of LLM calls that produced no project."""
        return self.no_project / self.read if self.read else 0.0

    @property
    def yield_rate(self) -> float:
        """Share of queued URLs that became a citation."""
        return self.cited / self.queued if self.queued else 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "feed": self.feed,
            "queued": self.queued,
            "pending": self.pending,
            "read": self.read,
            "extracted": self.extracted,
            "no_project": self.no_project,
            "thin": self.thin,
            "failed": self.failed,
            "cited": self.cited,
            "dated": self.dated,
            "waste": round(self.waste, 3),
            "yield_rate": round(self.yield_rate, 3),
        }


@dataclass
class Funnel:
    """The whole of stage 1, and the one number that pays for reading it."""

    feeds: list[FeedStat] = dc_field(default_factory=list)
    total_urls: int = 0
    reached_model: int = 0
    no_project: int = 0
    dated: int = 0

    @property
    def waste(self) -> float:
        return self.no_project / self.reached_model if self.reached_model else 0.0

    def ranked(self) -> list[FeedStat]:
        """Most wasteful first among feeds that have actually been read.

        Ordered by waste rather than volume, because the report exists to say
        which feed to reconsider. Ties break on `read` so a feed with two calls
        does not head the table, then on name for a total order.
        """
        return sorted(self.feeds, key=lambda f: (-f.waste, -f.read, f.feed))

    def as_json(self) -> dict[str, Any]:
        return {
            "total_urls": self.total_urls,
            "reached_model": self.reached_model,
            "no_project": self.no_project,
            "waste": round(self.waste, 3),
            "dated": self.dated,
            "feeds": [f.as_json() for f in self.ranked()],
        }


#: Calls a feed must have cost before its record is evidence of anything. Ten is
#: not statistical, it is just enough that "never once" stops being luck: a feed
#: read twice and citing nothing is indistinguishable from a feed read twice on
#: two quiet days.
MIN_READ_TO_JUDGE = 10

#: Below this share of reads producing a citation, a feed is earning its keep
#: thinly enough to be worth a look. Reported, never proposed — a feed that cites
#: something is contributing, and the threshold is a prompt to check rather than a
#: verdict.
LOW_YIELD = 0.15


@dataclass
class Verdict:
    """What to do about one feed, and the sentence that justifies it."""

    feed: str
    verdict: str
    why: str
    stat: FeedStat

    def as_json(self) -> dict[str, Any]:
        return {"feed": self.feed, "verdict": self.verdict, "why": self.why, **self.stat.as_json()}


def verdicts(report: Funnel, *, min_read: int = MIN_READ_TO_JUDGE) -> list[Verdict]:
    """Classify every feed. Only `retire` is a proposal.

    **Three different things look identical as "found a lot, cited nothing", and
    they need opposite responses.** Measured on the live database:

        applied-digital-newsroom   44 queued, 17 read, 17 none, 0 failed
        datacenterdynamics         39 queued,  1 read,  0 none, 12 failed
        utilitydive-archive        73 queued,  0 read,  0 none,  0 failed

    The first was read seventeen times and described no project once — retire it.
    The second cannot be read at all: its article pages sit behind Cloudflare, and
    `seed/feeds.toml` carries ten lines explaining why it stays anyway, because the
    headlines still say which projects exist. The third has never been read, so
    there is no evidence to judge it on and retiring it would be deciding on a
    sample of nothing.

    A queued-versus-cited ratio ranks all three the same and puts the one the
    config deliberately keeps at the top of the kill list. So the split is on
    **what happened after the fetch**, not on volume.
    """
    out: list[Verdict] = []
    for stat in report.feeds:
        if stat.feed == "(no feed)":
            # Search, archive sweeps and enrich. Real, and much larger than every
            # feed combined, but no feed list controls it — see `no_feed_share`.
            continue
        if stat.read == 0:
            if stat.failed:
                out.append(
                    Verdict(
                        stat.feed,
                        "cannot read",
                        f"{stat.failed} fetch failure(s), nothing read — a blocked feed, "
                        f"not a worthless one",
                        stat,
                    )
                )
            else:
                out.append(
                    Verdict(stat.feed, "not read yet", f"{stat.queued} queued, none read", stat)
                )
        elif stat.read < min_read:
            out.append(
                Verdict(stat.feed, "too few to judge", f"{stat.read} read, needs {min_read}", stat)
            )
        elif stat.cited == 0:
            # The queued count is the part that matters. Calls already made are
            # spent; what retiring a feed actually buys is not making the next
            # ones, so the proposal quotes the forward saving rather than the sunk
            # one.
            ahead = (
                f", and {stat.pending} more queued" if stat.pending else ", with none left queued"
            )
            out.append(
                Verdict(
                    stat.feed,
                    "retire",
                    f"{stat.read} call(s), {stat.no_project} found no project, "
                    f"not one backs a stored value{ahead}",
                    stat,
                )
            )
        elif stat.cited / stat.read < LOW_YIELD:
            out.append(
                Verdict(
                    stat.feed,
                    "low yield",
                    f"{stat.cited} citation(s) from {stat.read} call(s)",
                    stat,
                )
            )
        else:
            out.append(
                Verdict(
                    stat.feed, "keep", f"{stat.cited} citation(s) from {stat.read} call(s)", stat
                )
            )
    order = {
        "retire": 0,
        "low yield": 1,
        "cannot read": 2,
        "too few to judge": 3,
        "not read yet": 4,
    }
    return sorted(out, key=lambda v: (order.get(v.verdict, 9), -v.stat.read, v.feed))


def no_feed_share(report: Funnel) -> tuple[int, int]:
    """(wasted calls with no feed, wasted calls in total).

    Printed beside any retirement proposal, because it is the number that says how
    much retiring feeds can possibly achieve. On the live database 2,148 of the
    2,381 wasted calls came from URLs no feed found — search and archive sweeps —
    so the whole feed list accounts for about a tenth of the problem. Without this
    line the report reads as if pruning feeds fixes the 49%.
    """
    no_feed = next((f.no_project for f in report.feeds if f.feed == "(no feed)"), 0)
    return no_feed, report.no_project


def fetch_failures(session: Session, *, limit: int = 8) -> list[tuple[str, int]]:
    """Why fetches fail, commonest first — the silent-timeout audit.

    The 2026-08-12 review warned about a specific failure mode: a default timeout
    swallowing most of a run and nothing saying so ("2,000 requests, 1,600 timed
    out, and it never told you"). Worth checking rather than assuming, because the
    remedy differs entirely — a timeout means raise `fetch_timeout_s`, a 403 means
    escalate the fetcher, and a 404 means the URL is gone.

    Measured on the live database it came back negative: of 1,081 failures, 625 are
    HTTP 403 and 198 are 429 — deliberate blocks, which is what the `curl_cffi`
    rung already exists for — plus ~23 TLS failures and a handful of 5xx. Timeouts
    are not the problem here. Kept as a standing check, since the answer is a
    property of the corpus and the corpus grows.

    Grouped on the error text rather than parsed into categories: the strings come
    from httpx and curl_cffi and inventing a taxonomy over them would be a second
    thing to keep in sync with two libraries.
    """
    rows = session.execute(
        select(IngestUrl.error, func.count())
        .where(IngestUrl.status == "fetch_error", IngestUrl.error.is_not(None))
        .group_by(IngestUrl.error)
        .order_by(func.count().desc())
    ).all()
    return [(str(error)[:70], count) for error, count in rows[:limit]]


def survey(session: Session) -> Funnel:
    """Build the funnel from `ingest_url`, one grouped query per column."""
    out = Funnel()

    rows = session.execute(
        select(
            func.coalesce(IngestUrl.feed, "(no feed)"),
            IngestUrl.status,
            func.count(),
            func.sum(func.iif(IngestUrl.published_at.is_not(None), 1, 0)),
        ).group_by(IngestUrl.feed, IngestUrl.status)
    ).all()

    stats: dict[str, FeedStat] = {}
    for feed, status, count, dated in rows:
        stat = stats.setdefault(feed, FeedStat(feed=feed))
        stat.queued += count
        stat.dated += dated or 0
        out.total_urls += count
        out.dated += dated or 0
        if status in REACHED_THE_MODEL:
            stat.read += count
            out.reached_model += count
        if status == "ok":
            stat.extracted += count
        elif status == "no_project":
            stat.no_project += count
            out.no_project += count
        elif status == "thin_content":
            stat.thin += count
        elif status == "fetch_error":
            stat.failed += count
        elif status not in TERMINAL:
            stat.pending += count

    # Which of them actually became a citation. Joined rather than assumed: a URL
    # can extract `ok` and still leave no source row behind, when the project it
    # described was later merged away or the claims were all refused.
    cited = session.execute(
        select(func.coalesce(IngestUrl.feed, "(no feed)"), func.count())
        .select_from(IngestUrl)
        .join(Source, Source.url == IngestUrl.url)
        .group_by(IngestUrl.feed)
    ).all()
    for feed, count in cited:
        stats.setdefault(feed, FeedStat(feed=feed)).cited = count

    out.feeds = list(stats.values())
    return out


__all__ = [
    "LOW_YIELD",
    "MIN_READ_TO_JUDGE",
    "REACHED_THE_MODEL",
    "FeedStat",
    "Funnel",
    "Verdict",
    "fetch_failures",
    "no_feed_share",
    "survey",
    "verdicts",
]
