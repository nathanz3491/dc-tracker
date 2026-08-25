"""Reader view for a cited article, rendered from our own side.

**Why not the publisher's page in a frame.** That was the first attempt.
Measured across the fifteen most-cited publishers, **ten refuse to be framed** —
`X-Frame-Options: SAMEORIGIN`/`DENY` or a `frame-ancestors` directive — and those
ten carry 388 of their 689 citations. `datacenterdynamics.com`, the most-cited
publisher in the database at 150 citations, is one of them. No header of ours
overrides a publisher's, so a frame shows "refused to connect" for most of the
database no matter how it is configured.

**Why not the stripped text either.** The ingest path reduces every page to plain
text before the model sees it, deliberately — the evidence gate matches quotes
against exactly what the extractor read, so both sides must see identical input.
Correct for the gate, unreadable as a page: no headings, no paragraphs, no
images, navigation chrome left in the middle of the prose.

**What this does instead** is what every read-later tool converged on for this
exact problem — Firefox Reader View, Pocket, Instapaper, Wallabag, Miniflux: run
the arc90/Mozilla readability algorithm over the page's own HTML, keep the
article and throw away the furniture, and render the result under our own
stylesheet. Structure survives. Measured on six publishers including all the
frame-refusing ones: 450-1,200 ms, 14-26 paragraphs each, titles and links
intact.

**The stored quotes are marked in it**, which is the thing the reader actually
came for: not "here is the page" but "here is the sentence this number rests on".

Three independent things keep third-party HTML from becoming our problem:

1. it is **sanitized** — scripts, embeds, frames, forms, event handlers and
   every attribute outside a safe list are removed before it is stored;
2. it is served **into a sandboxed iframe with no `allow-` tokens**, so the
   document has an opaque origin and cannot script at all even if step 1 missed
   something;
3. the response carries its **own restrictive CSP** — `default-src 'none'`, with
   images the only thing that may load.

Any one of the three would do. Rendering somebody else's markup is the kind of
thing that deserves all three.
"""

from __future__ import annotations

import html as html_mod
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import select

from tracker.models import Source

log = logging.getLogger(__name__)

#: Ceiling on the stored article, against a pathological page. The cache
#: averages 11 KB an article; this is not a working limit.
MAX_CHARS: Final = 600_000

#: A highlight has to be worth drawing. Below this, a "quote" is a fragment that
#: would speckle the page with marks that mean nothing.
MIN_QUOTE_CHARS: Final = 24

#: What a sanitized article may contain. Everything else is dropped, including
#: every attribute not named here — `on*` handlers cannot survive an allowlist.
_ALLOWED_ATTRS: Final = frozenset({"href", "src", "alt", "title", "colspan", "rowspan"})

_UA: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class Reader:
    """One cited article, ready to render."""

    url: str
    title: str = ""
    body: str = ""
    #: `reader-cache` · `reader` · `text` · `excerpt` · `""`
    via: str = ""
    marks: int = 0
    error: str = ""


def load(
    session: Any,
    url: str,
    *,
    cache_dir: Path,
    reader_dir: Path,
    fetch: bool = True,
) -> Reader:
    """The reader view for `url`, with its stored quotes marked.

    **`url` must already be cited in the database.** That is the whole access
    rule: the console will read a page the pipeline chose, and nothing else. A
    reader that fetches whatever it is handed is a request forwarder pointed at
    the inside of whatever network the console runs on, and "it only reads" says
    nothing about where it may be pointed.
    """
    if not url:
        return Reader(url="", error="no url")
    rows = list(session.scalars(select(Source).where(Source.url == url)).all())
    if not rows:
        log.info("article refused: %s is not cited in the database", url)
        return Reader(url=url, error="that url is not cited in the database")

    quotes = _quotes(rows)
    cached = _read(reader_dir / _digest(url, ".html"))
    if cached:
        title, body = _split_title(cached)
        return _marked(url, title, body, "reader-cache", quotes)

    error = ""
    if fetch:
        title, body, error = _extract(url)
        if body:
            _write(reader_dir / _digest(url, ".html"), f"{title}\n{body}")
            return _marked(url, title, body, "reader", quotes)

    # Reader view unavailable — the library is absent, the fetch failed, or the
    # page had no article in it. The stored text still answers the question the
    # modal is open for, so fall back to it rather than to an empty pane.
    text, via = _stored_text(rows, url, cache_dir)
    if not text.strip():
        return Reader(url=url, error=error or "no text could be obtained")
    return _marked(url, "", _paragraphs(text), via, quotes)


# --- Getting the article ----------------------------------------------------


def _extract(url: str) -> tuple[str, str, str]:
    """`(title, sanitized body, error)` for one page, via readability.

    One ordinary request, no browser. Chromium is seconds and hundreds of
    megabytes, and this runs on a click — the same cost-proportionate ordering
    that keeps `--browser` behind a flag on the crawl. A page that only assembles
    itself under JavaScript falls back to the stored text.
    """
    try:
        from readability import Document
    except ImportError:
        return "", "", "reader view needs `pip install dc-tracker[reader]`"

    raw, error = _get(url)
    if not raw:
        return "", "", error
    # Repaired here rather than in `_decode`, because a double encoding is a
    # property of what the publisher wrote, not of how it reached us — a page
    # served from any path deserves the same fix.
    try:
        doc = Document(_strip_furniture(_demojibake(raw)))
        body = _debloat(_sanitize(doc.summary(html_partial=True), url))
        title = (doc.short_title() or "").strip()
    except Exception as exc:  # pragma: no cover - malformed markup
        log.warning("could not extract %s: %s", url, exc)
        return "", "", f"the page could not be parsed: {exc}"[:200]
    if not body.strip():
        return "", "", "no article was found on that page"
    return title, body[:MAX_CHARS], ""


def _get(url: str) -> tuple[str, str]:
    """The page's HTML. `curl_cffi` when it is installed, else `httpx`.

    The same reasoning the crawl's first escalation rung is built on: a browser's
    TLS fingerprint clears a class of WAF 403s that no User-Agent can, at the
    cost of one ordinary request.
    """
    try:
        from curl_cffi import requests as creq

        response = creq.get(url, impersonate="chrome", timeout=25)
        if response.status_code >= 400:
            return "", f"the publisher answered {response.status_code}"
        return _decode(response.content, response.headers.get("content-type", "")), ""
    except ImportError:
        pass
    except Exception as exc:
        log.warning("reader fetch of %s failed: %s", url, exc)
        return "", f"could not be fetched: {exc}"[:200]
    try:
        import httpx

        response = httpx.get(url, timeout=25, follow_redirects=True, headers={"user-agent": _UA})
        if response.status_code >= 400:
            return "", f"the publisher answered {response.status_code}"
        return _decode(response.content, response.headers.get("content-type", "")), ""
    except Exception as exc:
        log.warning("reader fetch of %s failed: %s", url, exc)
        return "", f"could not be fetched: {exc}"[:200]


#: `<meta charset>` in either spelling, within the head where it is required to be.
_META_CHARSET: Final = re.compile(rb"""<meta[^>]+charset=['"]?\s*([\w.-]+)""", re.I)


def _decode(raw: bytes, content_type: str) -> str:
    """Bytes to text, believing the page over the transport over the guess.

    **Decoded here rather than left to the HTTP client**, because the client
    guesses and both directions of the guess were observed in one survey:
    `datacenterfrontier.com` came back as "xAI�s AI Factories", and
    `datacenterknowledge.com` as "Cote dâ€™Ivoire" — the same
    apostrophe, lost two different ways.

    **Valid UTF-8 wins over any declaration, and that ordering is the whole
    point.** Believing the page sounds right and is wrong in practice:
    `datacenterknowledge.com` declares a Latin charset in its own `<meta>` and
    serves UTF-8 anyway. Decoding those bytes as Latin-1 cannot fail — every byte
    is a valid character — so a declaration-first order produces mojibake
    *silently*, with no exception to fall through. Multi-byte UTF-8 sequences, by
    contrast, are a shape that Latin-1 prose does not accidentally form, so a
    clean strict UTF-8 decode is evidence rather than a guess.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    found = _META_CHARSET.search(raw[:4096])
    for name in (
        found.group(1).decode("ascii", "ignore") if found else "",
        content_type.partition("charset=")[2].split(";")[0].strip(),
    ):
        if not name:
            continue
        try:
            return raw.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    # Not UTF-8 and nothing usable declared. Latin-1 cannot raise, so the article
    # survives even if a punctuation mark does not.
    return raw.decode("latin-1", errors="replace")


def _cp1252(first: int, last: int) -> str:
    """The characters Windows-1252 maps that byte range to.

    Five positions in `0x80-0x9f` are undefined in the codec, and decoders in the
    wild pass them through as the C1 control of the same number rather than
    failing. Both spellings are included, because both turn up in real mojibake —
    a closing curly quote arrives as `â€`, whose last character
    Windows-1252 has no name for.
    """
    out = []
    for b in range(first, last + 1):
        out.append(bytes([b]).decode("cp1252", errors="replace").replace("�", chr(b)))
    return "".join(out)


def _to_bytes(text: str) -> bytes:
    """The inverse of that decode, tolerating the same five positions."""
    out = bytearray()
    for char in text:
        try:
            out += char.encode("cp1252")
        except UnicodeEncodeError:
            if not "" <= char <= "":
                raise
            out.append(ord(char))
    return bytes(out)


#: UTF-8 that has already been decoded once as Windows-1252 — a leading byte
#: followed by exactly as many continuation bytes as its length announces.
#: `â€™` is one such run: the three bytes of a curly apostrophe,
#: shown as three characters.
#:
#: **Both classes are derived from the codec rather than typed out.** The literal
#: form is a line of mojibake in the source that no reviewer can check by eye,
#: and hand-copying it is how the continuation class ended up one character short
#: — matching two thirds of every three-byte run, which then failed to decode and
#: was silently left broken.
_LEAD_2, _LEAD_3 = _cp1252(0xC2, 0xDF), _cp1252(0xE0, 0xEF)
_CONT = _cp1252(0x80, 0xBF)
_MOJIBAKE: Final = re.compile(
    f"[{re.escape(_LEAD_3)}][{re.escape(_CONT)}]{{2}}|[{re.escape(_LEAD_2)}][{re.escape(_CONT)}]"
)


def _demojibake(text: str) -> str:
    """Undo a double encoding the publisher baked into their own page.

    `datacenterknowledge.com` serves "Cote dâ€™Ivoire", and it is not
    our decode that is wrong. The response says UTF-8, the bytes *are* valid
    UTF-8, and they encode those three characters literally: somewhere upstream
    an apostrophe was read as Windows-1252 and re-encoded, and the damage is in
    what the publisher actually serves.

    **Repaired one run at a time, not one document at a time.** The whole-document
    round trip is the obvious implementation and it silently does nothing on the
    real pages: a single character outside Windows-1252 anywhere on the page — a
    genuine curly quote, a CJK glyph in a name — makes `encode` raise, and the
    36 broken apostrophes elsewhere stay broken.

    **Each run is kept only if it round-trips**, which makes the repair
    self-checking rather than a guess: a sequence that was never double-encoded
    either fails to decode or is left exactly as it was.
    """
    if not _MOJIBAKE.search(text):
        return text

    def repair(match: re.Match[str]) -> str:
        try:
            return _to_bytes(match.group(0)).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return match.group(0)

    return _MOJIBAKE.sub(repair, text)


def _sanitize(fragment: str, base_url: str) -> str:
    """Third-party markup reduced to text, structure and images.

    An allowlist, not a blocklist. `on*` handlers, `javascript:` URLs, inline
    styles, `<script>`, `<iframe>`, `<object>` and `<form>` are not enumerated
    and removed one by one — everything outside `_ALLOWED_ATTRS` and the element
    cleaner's safe set is dropped, so a construct nobody thought of is dropped
    too.
    """
    from lxml_html_clean import Cleaner

    root = _parse(fragment)
    # Before cleaning, so `src`/`href` are absolute when the cleaner judges them.
    root.make_links_absolute(base_url, resolve_base_href=True)

    for img in root.iter("img"):
        # Lazy-loading puts the real image in a data attribute and leaves `src`
        # as a placeholder, which is why a naive extraction shows blank boxes.
        for attr in ("data-src", "data-original", "data-lazy-src"):
            if img.get(attr):
                img.set("src", img.get(attr))
                break
        if img.get("srcset") and not img.get("src"):
            img.set("src", img.get("srcset").split(",")[0].strip().split(" ")[0])

    cleaned = Cleaner(
        scripts=True,
        javascript=True,
        comments=True,
        style=True,
        inline_style=True,
        links=True,
        meta=True,
        page_structure=False,
        embedded=True,
        frames=True,
        forms=True,
        annoying_tags=True,
        safe_attrs_only=True,
        safe_attrs=_ALLOWED_ATTRS,
    ).clean_html(root)

    for link in cleaned.iter("a"):
        href = link.get("href") or ""
        if urlsplit(href).scheme not in {"http", "https"}:
            link.attrib.pop("href", None)
            continue
        # A link inside a sandboxed frame cannot navigate the console away, but
        # it can try to navigate itself; opening out is what a reader expects.
        link.set("target", "_blank")
        link.set("rel", "noreferrer noopener")
    for img in cleaned.iter("img"):
        if urlsplit(img.get("src") or "").scheme not in {"http", "https"}:
            img.getparent().remove(img)

    import lxml.html as _lh

    return _lh.tostring(cleaned, encoding="unicode")


# --- Throwing away the furniture --------------------------------------------
#
# Readability scores text density, which finds the article and is indifferent to
# what surrounds it *inside* the winning container. Two passes bracket it.
#
# **Before**, whole containers whose class or id names them as chrome are
# removed, so they cannot be scored at all. This is where the ad slots, share
# rails, cookie bars, comment threads and FAQ accordions go.
#
# **After**, what is left is trimmed at its seams: a "Related:" line embedded in
# the prose, a press release's contact block, the "Sign up at…" the publisher
# ends every post with. These live inside the article proper and no structural
# rule reaches them.

#: A container named as furniture. Matched on whole hyphen/underscore-separated
#: words, never as a substring — `ad` must not match `header` and `related` must
#: not match `unrelated-content`.
_JUNK_CONTAINER: Final = re.compile(
    r"(?:^|[-_\s])(?:ad|ads|advert|advertisement|adslot|banner|promo|promotion|sponsored|"
    r"share|sharing|social|share-bar|follow|newsletter|subscribe|signup|sign-up|"
    r"related|related-posts|more-from|recirc|recirculation|recommend|recommended|"
    r"comment|comments|disqus|livefyre|"
    r"nav|navbar|menu|breadcrumb|sidebar|footer|masthead|"
    r"cookie|consent|gdpr|paywall|modal|popup|lightbox|"
    r"faq|faqs|accordion|toc|table-of-contents|"
    r"tags|tag-list|byline-social|author-box|author-bio)(?:[-_\s]|$)",
    re.I,
)

#: A heading that ends the article, whatever follows it. Everything from here to
#: the end of the container goes — this is what removes a Q&A section, a
#: "Related stories" list, or a comment thread that survived the class pass.
_STOP_HEADING: Final = re.compile(
    r"^\s*(?:frequently asked questions?|faqs?|q\s*(?:&|and|&amp;)\s*a|"
    r"related(?:\s+(?:articles?|stories|posts?|reading|content))?|"
    r"more (?:from|on|stories)|you may(?: also)? like|recommended(?: for you)?|"
    r"read (?:more|next)|up next|also read|see also|"
    r"comments?|leave a (?:comment|reply)|share this|"
    r"about the authors?|tags?|newsletter|subscribe|sources?(?: and| &) methodology)"
    r"\s*[:.!]?\s*$",
    re.I,
)

#: A paragraph that is a signpost, not prose. Anchored, so a sentence *mentioning*
#: one of these words is untouched — only a line that opens with it goes.
_JUNK_LINE: Final = re.compile(
    r"^\s*(?:related|read more|read next|also read|see also|watch|watch now|more|"
    r"editor'?s note|advertisement|sponsored( content)?|"
    r"source|media contacts?|press contacts?|investor relations?|"
    r"have feedback|follow us|sign up|subscribe|share this|image[s]? (?:credit|suggest)|"
    r"photo credit|for more information)\b[\s:—-]*",
    re.I,
)

#: Once one of these starts, everything after it is contact details and legal —
#: the standard tail of a press release, which is most of `prnewswire.com`.
_TAIL_MARKER: Final = re.compile(
    r"^\s*(?:media contacts?|press contacts?|investor (?:relations?|contacts?)|"
    r"contacts?|for more information,? (?:visit|contact)|source\s+\S|"
    r"about\s+[A-Z][\w&.,' -]{2,40}$|have feedback on this article|"
    # Legal boilerplate. Always last, always the publisher talking about itself
    # rather than about the story.
    r"disclaimer|this article (?:by|is)\b|the views expressed|"
    r"companies (?:discussed|mentioned) in this article|"
    # The wire services' end-of-release markers, literally.
    r"\#\s*\#\s*\#|-\s*30\s*-$)",
    re.I,
)


#: Never removed by a name match, whatever their class says. WordPress writes the
#: page's entire state into the `<body>` class list — `wp-singular news-template-
#: default single single-news postid-2673 … no-sidebar` — so a name rule that can
#: reach the root will one day delete the document and report success.
#: `stackinfra.com` did exactly that: matched on `-sidebar`, in `no-sidebar`.
_STRUCTURAL: Final = frozenset({"html", "body", "main", "article"})

#: A container holding more than this share of the page's prose is the article,
#: whatever it is called. The backstop for the same class of mistake in general:
#: chrome is never most of what a page says.
_TOO_BIG_TO_BE_CHROME: Final = 0.4


#: How long a paragraph may be and still be cut as a tail marker. A legal
#: disclaimer runs to several sentences in one `<p>` — Simply Wall St's, carried
#: by every `yahoo.com` citation, is 280 characters — while a prose paragraph
#: opening "About the project…" is longer still and stays.
_TAIL_MAX: Final = 300


def _strip_furniture(raw: str) -> str:
    """Drop chrome containers before readability ever scores them.

    Readability ranks by text density, which reliably finds the article and is
    indifferent to what shares a container with it. Removing what is *named* as
    chrome first is the cheap half of the job, and it has to be done before
    scoring or an ad rail's word count competes with the prose.
    """
    try:
        import lxml.html

        root = lxml.html.document_fromstring(raw)
    except Exception:  # pragma: no cover - unparseable page, let readability try
        return raw
    for tag in ("script", "style", "noscript", "svg", "form", "aside", "nav", "footer"):
        for el in root.findall(f".//{tag}"):
            _drop(el)

    whole = len(" ".join(root.itertext()))
    for el in root.xpath("//*[@class or @id or @role or @data-testid]"):
        if el.tag in _STRUCTURAL or el.getparent() is None:
            continue
        token = " ".join(str(el.get(a) or "") for a in ("class", "id", "role", "data-testid"))
        if not _JUNK_CONTAINER.search(token):
            continue
        if whole and len(" ".join(el.itertext())) > whole * _TOO_BIG_TO_BE_CHROME:
            log.debug("kept %s despite %r: too much of the page to be chrome", el.tag, token)
            continue
        _drop(el)
    import lxml.html as _lh

    return _lh.tostring(root, encoding="unicode")


def _debloat(fragment: str) -> str:
    """Trim what readability kept but a reader would not have."""
    root = _parse(fragment)

    # Cut at the first stop heading, and at the first press-release tail marker.
    for el in list(root.iter()):
        text = " ".join(el.itertext()).strip()
        if not text:
            continue
        is_heading = el.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
        if (is_heading and _STOP_HEADING.match(text)) or (
            len(text) < _TAIL_MAX and _TAIL_MARKER.match(text)
        ):
            _cut_from(el)
            break

    # **A paragraph is judged by its whole text, inline children included.** The
    # first version skipped any element with children, so "For more information
    # about STACK, please visit <a>www.stackinfra.com</a>" survived every rule
    # written to catch it — the signpost and its link are one sentence to a
    # reader and a parent plus a child to a parser. Only block containers are
    # judged by their children, because dropping one would take real prose with it.
    for el in list(root.iter("p", "li", "h3", "h4", "h5", "h6", "strong", "em", "b")):
        if el.getparent() is None:
            continue
        text = " ".join(el.itertext()).strip()
        if not text or (len(text) < 200 and _JUNK_LINE.match(text)):
            _drop(el)

    import lxml.html as _lh

    return _lh.tostring(root, encoding="unicode")


def _parse(fragment: str) -> Any:
    """A fragment, however readability chose to hand it over.

    `summary(html_partial=True)` usually returns a `<div>`, but when the winning
    container was the whole document it returns `<html>…</html>` — and
    `fragment_fromstring` asserts rather than parsing that. Observed the first
    time the furniture pass emptied a page.
    """
    import lxml.html

    try:
        return lxml.html.fragment_fromstring(fragment, create_parent="div")
    except Exception:
        parsed = lxml.html.document_fromstring(fragment)
        body = parsed.find("body")
        return body if body is not None else parsed


def _drop(el: Any) -> None:
    """Remove an element, keeping the text that followed it."""
    parent = el.getparent()
    if parent is None:
        return
    if el.tail and el.tail.strip():
        previous = el.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def _cut_from(el: Any) -> None:
    """Remove `el` and everything that follows it in document order.

    **The ancestors are walked but never removed**, which is the whole
    difficulty. A first attempt reassigned `el = parent` and then deleted from
    `el` inclusive again, so each turn of the loop removed the parent still
    holding everything kept so far. Three publishers came back completely empty
    and the pass reported success — `prnewswire.com`, `stackinfra.com` and
    `yahoo.com`, each of which wraps the article in a container that a stop
    marker appears early inside.
    """
    parent = el.getparent()
    if parent is None:
        return
    for sibling in list(parent)[list(parent).index(el) :]:
        parent.remove(sibling)
    node = parent
    while (above := node.getparent()) is not None:
        for sibling in list(above)[list(above).index(node) + 1 :]:
            above.remove(sibling)
        node = above


def _stored_text(rows: list[Source], url: str, cache_dir: Path) -> tuple[str, str]:
    """The pipeline's own copy, for when the reader cannot be built."""
    from tracker.ingest.fetch import cache_path

    path = cache_path(url, cache_dir)
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - unreadable cache file
            log.warning("could not read %s: %s", path, exc)
        else:
            if text.strip():
                return text[:MAX_CHARS], "text"
    return max((r.excerpt or "" for r in rows), key=len, default=""), "excerpt"


def _paragraphs(text: str) -> str:
    """Plain text as paragraphs, so the fallback is still a page."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return "".join(f"<p>{html_mod.escape(b)}</p>" for b in blocks)


# --- Marking the evidence ---------------------------------------------------


def _quotes(rows: list[Source]) -> dict[str, str]:
    """Every stored quote for this URL, keyed by the field it evidenced.

    One URL is routinely cited by several projects, and the same sentence can
    carry a field for each. Keying by field collapses that to one highlight.
    """
    import json

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
            if isinstance(quote, str) and len(quote.strip()) >= MIN_QUOTE_CHARS:
                out.setdefault(str(name), quote.strip())
    return out


def _marked(url: str, title: str, body: str, via: str, quotes: dict[str, str]) -> Reader:
    """Wrap each stored quote in a `<mark>` where the article really says it.

    **Exact words, in order, and no fuzzy fallback.** The gate recovers a near
    miss when it is deciding whether to *store* a value, because the model
    resolves pronouns while quoting. Drawing a highlight makes a different claim
    — "this sentence is the evidence" — so if the page has changed since it was
    cited, no mark is the honest outcome. Only whitespace and case are forgiven,
    which is the difference between one rendering of a sentence and another,
    never the difference between two sentences.
    """
    if not quotes or not body:
        return Reader(url=url, title=title, body=body, via=via)
    root = _parse(body)

    marks = 0
    for field, quote in quotes.items():
        pattern = re.compile(r"\s+".join(map(re.escape, quote.split())), re.I)
        if _mark_once(root, pattern, field):
            marks += 1
    import lxml.html as _lh

    return Reader(
        url=url,
        title=title,
        body=_lh.tostring(root, encoding="unicode"),
        via=via,
        marks=marks,
    )


def _mark_once(root: Any, pattern: re.Pattern[str], field: str) -> bool:
    """Mark the first occurrence lying wholly inside one text node.

    A quote broken across an inline `<a>` is left unmarked rather than
    reconstructed. Splitting an element tree around a partial match is where a
    highlighter starts corrupting the document it is annotating, and the reader
    loses one mark instead of a paragraph.
    """
    import lxml.html

    for el in root.iter():
        if el.tag in {"mark", "script", "style"}:
            continue
        if el.text:
            found = pattern.search(el.text)
            if found:
                mark = lxml.html.Element("mark")
                mark.set("data-field", field)
                mark.text = el.text[found.start() : found.end()]
                mark.tail = el.text[found.end() :]
                el.text = el.text[: found.start()]
                el.insert(0, mark)
                return True
        for index, child in enumerate(el):
            if not child.tail:
                continue
            found = pattern.search(child.tail)
            if found:
                mark = lxml.html.Element("mark")
                mark.set("data-field", field)
                mark.text = child.tail[found.start() : found.end()]
                mark.tail = child.tail[found.end() :]
                child.tail = child.tail[: found.start()]
                el.insert(index + 1, mark)
                return True
    return False


# --- Rendering --------------------------------------------------------------

#: Why the page looks the way it does, stated where the reader is looking.
_VIA_NOTE: Final = {
    "reader": "Reader view of the publisher's page, fetched just now and saved.",
    "reader-cache": "Reader view of the publisher's page, from our saved copy.",
    "text": "Reader view was unavailable, so this is the plain text the pipeline read.",
    "excerpt": "Only the stored excerpt — the full article could not be read.",
}


def render(found: Reader, *, dark: bool = False) -> str:
    """A complete, self-contained document for the sandboxed frame.

    Its own CSP, and a strict one: `default-src 'none'` with images the only
    thing that may load. The frame that holds it also carries `sandbox` with no
    `allow-` tokens, so this document has an opaque origin and cannot run script
    even if the sanitizer missed something.
    """
    note = _VIA_NOTE.get(found.via, "")
    if found.error and not found.body:
        body = f"<p class='dc-fail'>{html_mod.escape(found.error)}</p>"
        note = ""
    else:
        body = found.body
    heading = f"<h1 class='dc-title'>{html_mod.escape(found.title)}</h1>" if found.title else ""
    marks = (
        f"<span class='dc-count'>{found.marks} quoted sentence"
        f"{'' if found.marks == 1 else 's'} marked</span>"
        if found.marks
        else ""
    )
    return f"""<!doctype html>
<html lang="en" data-theme="{"dark" if dark else "light"}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- Images still load from the publisher's CDN, which is the one thing here that
     touches their servers from the reader's browser. Sending no referrer means
     they learn an IP fetched an asset, not which article was being read. -->
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src https: data:; style-src 'unsafe-inline'">
<title>{html_mod.escape(found.title or found.url)}</title>
<style>{_CSS}</style>
</head>
<body>
<article class="dc-doc">
  {heading}
  <div class="dc-meta">{html_mod.escape(note)}{marks}</div>
  {body}
</article>
</body>
</html>"""


#: Deliberately not the console's stylesheet. This document is a different kind
#: of thing — somebody else's prose — and it is read, not scanned. A measure
#: around 68ch and a larger body size is the one place in this project where
#: reading beats density.
_CSS: Final = """
:root {
  color-scheme: light;
  --bg: #faf6ef; --ink: #24201b; --dim: #6b625a; --line: #e2d9cb;
  --mark: rgba(79,132,58,0.24); --markline: #4f843a;
}
html[data-theme="dark"] {
  color-scheme: dark;
  --bg: #1b1410; --ink: #f3eadb; --dim: #a2968a; --line: #3a2f27;
  --mark: rgba(139,193,113,0.22); --markline: #8bc171;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 17px/1.7 ui-serif, Georgia, "Times New Roman", serif;
  -webkit-text-size-adjust: 100%;
}
.dc-doc { max-width: 68ch; margin: 0 auto; padding: 32px 24px 64px; }
.dc-title {
  font-size: 30px; line-height: 1.25; margin: 0 0 10px;
  font-family: ui-sans-serif, system-ui, sans-serif; font-weight: 650;
}
.dc-meta {
  display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 28px; padding-bottom: 14px;
  border-bottom: 1px solid var(--line);
  font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; color: var(--dim);
}
.dc-count::before { content: "· "; }
.dc-fail { color: var(--dim); font-style: italic; }
p { margin: 0 0 1.15em; }
h1, h2, h3, h4 {
  font-family: ui-sans-serif, system-ui, sans-serif; line-height: 1.3;
  margin: 1.8em 0 0.5em; font-weight: 650;
}
h2 { font-size: 21px; } h3 { font-size: 18px; } h4 { font-size: 16px; }
a { color: inherit; text-underline-offset: 2px; }
img, figure, video { max-width: 100%; height: auto; margin: 1.5em auto; display: block; }
figcaption, small { font-size: 13px; color: var(--dim); }
blockquote {
  margin: 1.5em 0; padding-left: 16px; border-left: 3px solid var(--line); color: var(--dim);
}
ul, ol { padding-left: 1.4em; margin: 0 0 1.15em; }
li { margin-bottom: 0.4em; }
pre, code { font-family: ui-monospace, monospace; font-size: 14px; }
pre { overflow-x: auto; padding: 12px; border: 1px solid var(--line); border-radius: 6px; }
table { width: 100%; border-collapse: collapse; margin: 1.5em 0; font-size: 15px; }
th, td { border: 1px solid var(--line); padding: 6px 9px; text-align: left; }
hr { border: 0; border-top: 1px solid var(--line); margin: 2em 0; }
/* The evidence, marked in the text it came from. Tinted rather than filled: a
   solid highlight over a paragraph reads as a text selection. */
mark {
  background: var(--mark); color: inherit;
  box-shadow: inset 0 -2px 0 var(--markline); border-radius: 2px; padding: 1px 0;
}
"""


# --- Small helpers ----------------------------------------------------------


def _digest(url: str, suffix: str) -> str:
    import hashlib

    return hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest() + suffix


def _read(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - unreadable cache file
        log.warning("could not read %s: %s", path, exc)
        return ""


def _write(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unwritable cache dir
        log.warning("could not cache %s: %s", path, exc)


def _split_title(cached: str) -> tuple[str, str]:
    title, _, body = cached.partition("\n")
    return title, body


__all__ = ["MAX_CHARS", "MIN_QUOTE_CHARS", "Reader", "load", "render"]
