"""The text behind a cited URL, served to the console from our own side.

**Why this is not an iframe.** The sources modal originally framed the live page.
Measured across the fifteen most-cited publishers, ten of them refuse to be
framed — `X-Frame-Options: SAMEORIGIN`, `DENY`, or a `frame-ancestors` directive
— and those ten carry 388 of their 689 citations. No header of ours overrides a
publisher's, so a frame-first modal shows "refused to connect" for the majority
of the database. `datacenterdynamics.com`, the single most-cited publisher at
150 citations, is one of them.

**What is served instead is what the pipeline actually read**, which is the more
useful artefact anyway. A live page may have been edited since it was cited; the
cached text is the evidence the values rest on. The stored quotes are located in
it and returned as spans, so the drawer can show *which sentence* carried a
field rather than leaving the reader to hunt for it.

Locating a quote is a judgement — normalisation, folding, offsets — so it
happens here against the same helpers the evidence gate uses, never in the
browser. A second implementation would eventually disagree with the gate about
what a source says, and nothing would report the drift.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import select

from tracker.models import Source

log = logging.getLogger(__name__)

#: How much text to send. The cache averages 11 KB an article; this is a ceiling
#: against a pathological page, not a working limit.
MAX_CHARS: Final = 400_000

#: A highlight has to be worth drawing. Below this a "quote" is a fragment that
#: would speckle the reader with meaningless marks.
MIN_SPAN_CHARS: Final = 24


@dataclass(frozen=True, slots=True)
class Article:
    """Text for one cited URL, and where its quotes sit in it."""

    url: str
    text: str = ""
    #: `cache` · `fetch` · `excerpt` · `""` when nothing was obtained.
    via: str = ""
    #: `(start, end, field)` into `text`, non-overlapping, in document order.
    spans: tuple[tuple[int, int, str], ...] = ()
    error: str = ""
    fields: tuple[str, ...] = ()

    def to_json_object(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "text": self.text,
            "via": self.via,
            "spans": [[s, e, f] for s, e, f in self.spans],
            "error": self.error,
            "fields": list(self.fields),
        }


def load(
    session: Any,
    url: str,
    *,
    cache_dir: Path,
    fetch: bool = True,
) -> Article:
    """The article behind `url`, from the cache, else fetched, else its excerpt.

    **`url` must already be cited in the database.** That is the whole access
    rule: the console will read a page the pipeline chose, and nothing else. An
    endpoint that fetched whatever it was handed would be a request forwarder
    pointed at the inside of whatever network the console runs on.
    """
    if not url:
        return Article(url="", error="no url")
    rows = list(session.scalars(select(Source).where(Source.url == url)).all())
    if not rows:
        log.info("article refused: %s is not cited in the database", url)
        return Article(url=url, error="that url is not cited in the database")

    from tracker.ingest.fetch import cache_path

    path = cache_path(url, cache_dir)
    text, via, error = "", "", ""
    if path.is_file():
        try:
            text, via = path.read_text(encoding="utf-8", errors="replace"), "cache"
        except OSError as exc:  # pragma: no cover - unreadable cache file
            log.warning("could not read %s: %s", path, exc)
    if not text.strip() and fetch:
        text, error = _fetch(url, cache_dir)
        via = "fetch" if text else ""
    if not text.strip():
        # The excerpt is a few hundred characters, but it is never nothing, and
        # saying so beats an empty pane that looks like a bug.
        excerpt = max((r.excerpt or "" for r in rows), key=len, default="")
        if excerpt.strip():
            text, via = excerpt, "excerpt"
    if not text.strip():
        return Article(url=url, error=error or "no text could be obtained")

    text = text[:MAX_CHARS]
    quotes = _quotes(rows)
    return Article(
        url=url,
        text=text,
        via=via,
        spans=_locate(text, quotes),
        error=error,
        fields=tuple(sorted(quotes)),
    )


def _fetch(url: str, cache_dir: Path) -> tuple[str, str]:
    """One page through the cheap rungs, cached on the way past.

    **The browser rung is deliberately excluded.** Chromium is seconds and
    hundreds of megabytes, and this runs on a click in a read-only console —
    the same cost-proportionate reasoning that keeps `--browser` behind a flag
    on the crawl. A page that only renders under JavaScript reports that it
    could not be read, and the reader still has "open in a new tab".
    """
    from tracker.ingest.crawl import _write_cache
    from tracker.ingest.fetch import escalation_ladder, fetch_all

    try:
        results = asyncio.run(
            fetch_all([url], escalate=escalation_ladder(browser=False))
        )
    except Exception as exc:  # pragma: no cover - network/runtime failure
        log.warning("on-demand fetch of %s failed: %s", url, exc)
        return "", f"could not be fetched: {exc}"[:200]
    result = results[0] if results else None
    if result is None or not result.ok or not result.markdown:
        reason = (getattr(result, "error", "") or "no text returned") if result else "no result"
        return "", f"could not be fetched: {reason}"[:200]
    try:
        _write_cache([result], cache_dir)
    except OSError as exc:  # pragma: no cover - unwritable cache dir
        log.warning("could not cache %s: %s", url, exc)
    return result.markdown, ""


def _quotes(rows: list[Source]) -> dict[str, str]:
    """Every stored quote for this URL, keyed by the field it evidenced.

    One URL can be cited by several projects, and the same sentence can carry a
    field for each of them. Keying by field collapses that to one highlight.
    """
    out: dict[str, str] = {}
    for row in rows:
        raw = getattr(row, "quotes", None)
        if not raw:
            continue
        try:
            blob = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(blob, dict):
            continue
        for name, quote in blob.items():
            if isinstance(quote, str) and quote.strip():
                out.setdefault(str(name), quote)
    return out


def _locate(text: str, quotes: dict[str, str]) -> tuple[tuple[int, int, str], ...]:
    """Where each quote sits in `text`, exactly — never approximately.

    An exact find after normalisation, not the gate's fuzzy recovery. The stored
    quote is already the *article's* own words, so if it is absent from the text
    in front of us the page has changed since it was cited, and drawing a
    highlight over the nearest similar sentence would assert evidence that is no
    longer there. A missing highlight is the honest outcome.
    """
    from tracker.ingest.crawl import _normalize_for_match, _normalize_with_offsets

    if not quotes:
        return ()
    haystack, offsets = _normalize_with_offsets(text)
    if not haystack:
        return ()
    found: list[tuple[int, int, str]] = []
    for name, quote in quotes.items():
        needle = _normalize_for_match(quote)
        if len(needle) < MIN_SPAN_CHARS:
            continue
        at = haystack.find(needle)
        if at < 0:
            continue
        found.append((offsets[at], offsets[at + len(needle) - 1] + 1, name))
    found.sort()
    # Two fields often rest on the same sentence, and nested `<mark>`s render as
    # a darker band that reads like a third kind of highlight. First span wins.
    kept: list[tuple[int, int, str]] = []
    for start, end, name in found:
        if kept and start < kept[-1][1]:
            continue
        kept.append((start, end, name))
    return tuple(kept)


__all__ = ["MAX_CHARS", "MIN_SPAN_CHARS", "Article", "load"]
