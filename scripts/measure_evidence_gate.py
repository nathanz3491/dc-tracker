#!/usr/bin/env python
"""Re-run the three measurements the evidence gate's thresholds rest on.

Every number in `crawl.MIN_PROSE_CHARS`, `crawl.MIN_RUN_CHARS` and
`crawl.MIN_RUN_FRACTION` came from measuring a real corpus. Corpora change, so
those docstrings have a shelf life — this is how you check whether they still
hold instead of taking a comment's word for it.

    python scripts/measure_evidence_gate.py
    python scripts/measure_evidence_gate.py --root /path/to/install

Reads the article cache and the database, writes nothing, spends nothing, and
needs no API key. Both live outside the repository, so this cannot run in CI;
that is the point. It measures the corpus you actually have.

`--root` exists because the cache and database belong to an *install*, not a
checkout — running this from a git worktree otherwise finds an empty cache and
reports nothing, which reads like a corpus with no articles in it.

Three sections:

1. **Thin content.** Prose length per `source_type`, and what `MIN_PROSE_CHARS`
   refuses. A page fetched successfully whose body is navigation furniture is
   refused before the LLM call, and the floor is only defensible while trade
   press sits far above it and real short filings sit above it too.

2. **Exact-match share.** How often a stored quote is a plain substring of its
   own article — i.e. how often `_verbatim_run` recovery is needed at all. The
   diagnosis behind `docs/plan-evidence-gate.md` measured 100% exact on 27
   quotes and concluded the recovery thresholds were not the base-rate driver of
   the failures being reported. If that share ever falls, section D of that plan
   (relaxing the matching) has evidence behind it; until then it does not.

3. **The negative control.** Every stored quote, tested against an *unrelated*
   article. None may cross the gate. This is the experiment `MIN_RUN_CHARS` was
   tuned against, and the plan makes repeating it mandatory for any change to the
   matching: a gate that accepts a real sentence from a story about a different
   site is not a gate.

   "Unrelated" has to mean a different *publisher*, and getting that wrong is how
   this measurement lies to you. Pairing naively across the whole cache reported
   three crossings on first run, and all three were one company's own boilerplate
   recurring in its own documents — two Applied Blockchain filings under one CIK,
   two H5 Data Centers pages under one domain. A sentence like "H5 Data Centers, a
   national colocation and wholesale data center provider" really is verbatim in
   both pages, so accepting it is not fabrication and counting it as a control
   failure would send you to raise a threshold that is not broken.

   It is still worth knowing, so it is reported on its own line. It names the
   gate's actual blind spot: boilerplate is verbatim everywhere a company
   publishes, so quoting it proves the sentence was published and proves nothing
   about *which site* it describes. That is a semantic failure no substring test
   can catch, and it is the exact class `tracker logic check --audit` exists for.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tracker.ingest.crawl import (  # noqa: E402
    MIN_PROSE_CHARS,
    MIN_RUN_CHARS,
    MIN_RUN_FRACTION,
    _normalize_for_match,
    _verbatim_run,
    classify_source_type,
    operator_hosts,
    prose_length,
)

#: How many (quote, unrelated article) pairs to test. The full cross product is
#: quadratic and the matcher is not cheap; a few thousand pairs is already far
#: more than the 131 the thresholds were originally tuned against.
NEGATIVE_PAIRS = 4000


#: Set by `--root`. None means "ask the package where it is installed".
_ROOT: Path | None = None


def _install_root() -> Path:
    if _ROOT is not None:
        return _ROOT
    from tracker.config import install_root

    return install_root()


def _cache_dir() -> Path:
    return _install_root() / ".cache" / "articles"


def _db_path() -> Path:
    return _install_root() / "data" / "tracker.db"


def cached_articles() -> dict[str, str]:
    """url -> article text, for every URL in the database that is still cached."""
    cache, db = _cache_dir(), _db_path()
    if not cache.is_dir():
        sys.exit(f"no article cache at {cache}; run a crawl first")
    if not db.exists():
        sys.exit(f"no database at {db}")

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    urls: set[str] = set()
    for table in ("ingest_url", "source"):
        urls |= {row[0] for row in con.execute(f"SELECT url FROM {table}") if row[0]}
    con.close()

    out: dict[str, str] = {}
    for url in sorted(urls):
        digest = hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()
        path = cache / f"{digest}.txt"
        if path.exists():
            out[url] = path.read_text(encoding="utf-8", errors="replace")
    return out


def stored_quotes() -> list[tuple[str, str, str]]:
    """(url, field, quote) for every per-field quote and risk quote on record."""
    con = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    out: list[tuple[str, str, str]] = []
    for url, blob in con.execute("SELECT url, quotes FROM source WHERE quotes IS NOT NULL"):
        try:
            parsed = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            out.extend(
                (url, name, quote)
                for name, quote in parsed.items()
                if isinstance(quote, str) and quote.strip()
            )
    for url, quote in con.execute(
        "SELECT s.url, r.quote FROM risk r JOIN source s ON s.id = r.source_id "
        "WHERE r.quote IS NOT NULL"
    ):
        if url and quote and quote.strip():
            out.append((url, "risk", quote))
    con.close()
    return out


def report_thin_content(articles: dict[str, str]) -> None:
    print(
        f"\n{'=' * 78}\n1. Thin content — what MIN_PROSE_CHARS = {MIN_PROSE_CHARS} refuses\n{'=' * 78}"
    )
    hosts = operator_hosts()
    by_type: dict[str, list[int]] = defaultdict(list)
    refused: list[tuple[int, str, str]] = []
    for url, text in articles.items():
        kind = classify_source_type(url, operator_hosts=hosts)
        length = prose_length(text)
        by_type[kind].append(length)
        if length < MIN_PROSE_CHARS:
            refused.append((length, kind, url))

    print(f"\n{'source_type':<18}{'n':>5}{'min':>8}{'p05':>8}{'p25':>8}{'median':>8}")
    for kind, lengths in sorted(by_type.items()):
        lengths = sorted(lengths)
        n = len(lengths)
        print(
            f"{kind:<18}{n:>5}{lengths[0]:>8}"
            f"{lengths[min(n - 1, int(0.05 * n))]:>8}"
            f"{lengths[min(n - 1, int(0.25 * n))]:>8}"
            f"{int(statistics.median(lengths)):>8}"
        )

    total = len(articles)
    print(f"\nrefused: {len(refused)} of {total} ({len(refused) / max(1, total):.1%})")
    print("Read these. Every one should be navigation furniture, not an article:")
    for length, kind, url in sorted(refused)[:25]:
        print(f"  prose {length:>5}  {kind:<15} {url[:82]}")

    kept = sorted(v for group in by_type.values() for v in group if v >= MIN_PROSE_CHARS)
    if kept:
        print(f"\nthinnest page KEPT: {kept[0]} chars of prose — the margin above the floor.")


def report_exact_match(articles: dict[str, str], quotes: list[tuple[str, str, str]]) -> None:
    print(f"\n{'=' * 78}\n2. Exact-match share — is recovery load-bearing?\n{'=' * 78}")
    exact = recovered = lost = 0
    shortfalls: list[str] = []
    for url, field, quote in quotes:
        article = articles.get(url)
        if article is None:
            continue
        if _normalize_for_match(quote) in _normalize_for_match(article):
            exact += 1
            continue
        run = _verbatim_run(quote, article)
        if run.text is not None:
            recovered += 1
        else:
            lost += 1
            if len(shortfalls) < 10:
                shortfalls.append(f"  {field:<16} {run.shortfall()}  {quote[:70]}")

    tested = exact + recovered + lost
    if not tested:
        print("no stored quote could be paired with its cached article; nothing to measure")
        return
    print(f"\n{tested} stored quotes tested against their own article:")
    print(f"  exact substring   {exact:>5}  ({exact / tested:.1%})")
    print(f"  needed recovery   {recovered:>5}  ({recovered / tested:.1%})")
    print(f"  no longer matches {lost:>5}  ({lost / tested:.1%})   <- article edited since ingest")
    if shortfalls:
        print("\n  the closest misses:")
        print("\n".join(shortfalls))
    print(
        "\nA high exact share means the matching is not what is refusing values, and"
        "\nsection D of docs/plan-evidence-gate.md has nothing to act on yet."
    )


def publisher(url: str) -> str:
    """Who published this, at the grain that makes two documents 'related'.

    The host, except on EDGAR where every filing shares one — there the CIK in
    the path is the filer, so `/data/1144879/` separates Applied Blockchain's own
    filings from everybody else's.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    if "sec.gov" in host:
        segments = [s for s in parts.path.split("/") if s]
        if "data" in segments:
            index = segments.index("data")
            if index + 1 < len(segments):
                return f"sec.gov/{segments[index + 1]}"
    return host


def report_negative_control(articles: dict[str, str], quotes: list[tuple[str, str, str]]) -> None:
    print(
        f"\n{'=' * 78}\n3. Negative control — MIN_RUN_CHARS={MIN_RUN_CHARS}, "
        f"MIN_RUN_FRACTION={MIN_RUN_FRACTION}\n{'=' * 78}"
    )
    urls = sorted(articles)
    if len(urls) < 2 or not quotes:
        print("need at least two cached articles and one stored quote")
        return

    crossings: list[str] = []
    boilerplate: list[str] = []
    tested = same_publisher = 0
    # Deterministic pairing, no RNG: quote i against the article that many steps
    # along, skipping its own. Reproducible run to run, which a random sample
    # would not be, and a failure has to be reproducible to be fixable.
    for index, (own_url, field, quote) in enumerate(quotes):
        if tested >= NEGATIVE_PAIRS:
            break
        for step in (1, 7, 53):
            other = urls[(index * step + step) % len(urls)]
            if other == own_url:
                continue
            related = publisher(other) == publisher(own_url)
            if not related:
                tested += 1
            else:
                same_publisher += 1
            run = _verbatim_run(quote, articles[other])
            if run.text is None:
                continue
            line = (
                f"  {field} quote from {own_url[:52]}\n"
                f"    matched in {other[:62]}\n"
                f"    {run.shortfall()} -> {run.text[:88]!r}"
            )
            (boilerplate if related else crossings).append(line)

    print(f"\n{tested} (quote, unrelated-publisher article) pairs tested")
    if crossings:
        print(f"\n*** {len(crossings)} CROSSED. The gate accepted a quote from another story. ***")
        print("\n".join(crossings[:10]))
        print("\nThis is a failure, not a statistic. Raise the thresholds or revert.")
    else:
        print("0 crossed — the control holds at these thresholds.")

    print(f"\n{same_publisher} pairs from the SAME publisher were measured separately.")
    if boilerplate:
        print(
            f"{len(boilerplate)} of those matched — one company's boilerplate recurring in its\n"
            "own documents. Not a control failure: the sentence really is in both pages, so\n"
            "the gate is right that somebody published it. It is the gate's blind spot,\n"
            "though — quoting boilerplate proves publication and not which site it describes,\n"
            "which is a semantic failure only `tracker logic check --audit` can catch."
        )
        print("\n".join(boilerplate[:5]))
    else:
        print("None of those matched.")


def main() -> None:
    global _ROOT
    argv = sys.argv[1:]
    if argv and argv[0] == "--root":
        if len(argv) < 2:
            sys.exit("--root needs a path")
        _ROOT = Path(argv[1]).resolve()
        print(f"reading the corpus under {_ROOT}")

    articles = cached_articles()
    quotes = stored_quotes()
    print(f"{len(articles)} cached articles, {len(quotes)} stored quotes")
    report_thin_content(articles)
    report_exact_match(articles, quotes)
    report_negative_control(articles, quotes)


if __name__ == "__main__":
    main()
