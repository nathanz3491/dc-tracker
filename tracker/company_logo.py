"""A company's logo for its page: found on its own website, fetched once, cached.

**Where the website comes from.** Nothing in the database records a company's
domain, but its campuses' citations usually include its own newsroom or filings —
`news.microsoft.com`, `compassdatacenters.com`, `crusoe.ai`. `website` picks the
cited domain whose name *is* the company's (letters only), preferring `.com` when a
company is cited under several country domains, and says nothing rather than
guess when none does.

The biggest tenants are the exception: Meta, Amazon and Oracle are cited through
their landlords' press releases and almost never through their own sites, so the
rule above finds nothing for exactly the names readers open most. `KNOWN_SITES`
names those few by hand; it is a list of the obvious, not a directory.

**Where the logo comes from.** That site's own declared icon — `apple-touch-icon`
first, because it is drawn at 180px rather than 16 — then any `<link rel="icon">`,
then `/favicon.ico`. One request for the page and one for the image, made by the
console's server with the project's own User-Agent, and cached on disk for
`TTL_DAYS`; a company with no findable icon is remembered too, so the page does
not ask again on every visit. The console's content policy allows images only from
itself, which is why the server fetches and serves it rather than the browser.

The page falls back to an initials badge whenever this returns nothing.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Final
from urllib.parse import urljoin, urlsplit

#: How long a fetched logo, or the absence of one, is kept.
TTL_DAYS: Final[int] = 30

#: Largest image served. A logo is a few kilobytes; anything bigger is not one.
MAX_BYTES: Final[int] = 300_000

#: Companies whose own site is rarely cited, keyed by `dedup.company_key`.
KNOWN_SITES: Final[dict[str, str]] = {
    "amazon": "aboutamazon.com",
    "amazon web services": "aboutamazon.com",
    "google": "google.com",
    "meta": "meta.com",
    "microsoft": "microsoft.com",
    "oracle": "oracle.com",
    "openai": "openai.com",
    "xai": "x.ai",
    "qts": "qtsdatacenters.com",
    "qts data centers": "qtsdatacenters.com",
}

_ICON_LINK = re.compile(r"<link\b[^>]*>", re.I)
_REL = re.compile(r"""\brel\s*=\s*["']?([^"'>]+)""", re.I)
_HREF = re.compile(r"""\bhref\s*=\s*["']?([^"'\s>]+)""", re.I)
_TYPES: Final[dict[str, str]] = {
    "image/png": "png",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/svg+xml": "svg",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def website(company_key: str, urls: list[str]) -> str | None:
    """The cited domain that is the company's own, or None.

    A domain qualifies when its name without the public suffix, letters only,
    equals the company key letters only (`compassdatacenters.com` for
    "compass datacenters", `x.ai` for "xai"), or begins with the key's first word
    of four letters or more (`microsoft.com` for "microsoft corp"). The most-cited
    qualifying domain wins.
    """
    from tracker.confidence import registrable_domain

    if (company_key or "") in KNOWN_SITES:
        return KNOWN_SITES[company_key]
    whole = _letters(company_key)
    first = _letters((company_key or "").split(" ")[0])
    if not whole:
        return None
    exact: Counter[str] = Counter()
    loose: Counter[str] = Counter()
    for url in urls:
        domain = registrable_domain(url)
        if not domain:
            continue
        name = domain.rsplit(".", 1)[0] if domain.count(".") == 1 else domain.split(".")[0]
        if _letters(domain) == whole or _letters(name) == whole:
            exact[domain] += 1
        elif len(first) >= 4 and _letters(name).startswith(first):
            loose[domain] += 1
    best = exact or loose
    if not best:
        return None
    # Most cited first; between equals — and over a country domain cited more,
    # `google.cn` against `google.com` — the `.com`.
    return max(best, key=lambda d: (d.endswith(".com"), best[d]))


def _icon_candidates(html: str, base: str) -> list[str]:
    touch: list[str] = []
    icons: list[str] = []
    for tag in _ICON_LINK.findall(html[:200_000]):
        rel = _REL.search(tag)
        href = _HREF.search(tag)
        if not rel or not href:
            continue
        rels = rel.group(1).lower().split()
        if "apple-touch-icon" in rels or "apple-touch-icon-precomposed" in rels:
            touch.append(urljoin(base, href.group(1)))
        elif "icon" in rels:
            icons.append(urljoin(base, href.group(1)))
    # A few of each at most: a page declaring a dozen sizes should not cost a dozen
    # requests on a reader's first visit.
    return touch[:2] + icons[:2] + [urljoin(base, "/favicon.ico")]


def _fetch_logo(domain: str, user_agent: str, timeout: float) -> tuple[bytes, str] | None:
    import httpx

    headers = {"User-Agent": user_agent}
    base = f"https://{domain}/"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            try:
                page = client.get(base)
                html = page.text if page.status_code < 400 else ""
                base = str(page.url) if page.status_code < 400 else base
            except httpx.HTTPError:
                html = ""
            for url in _icon_candidates(html, base):
                if urlsplit(url).scheme not in ("http", "https"):
                    continue
                try:
                    got = client.get(url)
                except httpx.HTTPError:
                    continue
                kind = got.headers.get("content-type", "").split(";")[0].strip().lower()
                if got.status_code < 400 and kind in _TYPES and 0 < len(got.content) <= MAX_BYTES:
                    return got.content, kind
    except httpx.HTTPError:
        return None
    return None


def logo(
    slug: str,
    domain: str | None,
    *,
    cache: Path,
    user_agent: str,
    timeout: float = 8.0,
    fetch=_fetch_logo,
) -> tuple[bytes, str] | None:
    """The logo bytes and content type for one company, from cache or its website."""
    cache.mkdir(parents=True, exist_ok=True)
    meta_path = cache / f"{slug}.json"
    now = time.time()
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if now - meta.get("at", 0) < TTL_DAYS * 86_400 and meta.get("domain") == domain:
            if not meta.get("file"):
                return None
            data = (cache / meta["file"]).read_bytes()
            return data, meta["type"]
    except (OSError, ValueError, KeyError):
        pass
    found = fetch(domain, user_agent, timeout) if domain else None
    record: dict[str, object] = {"at": now, "domain": domain, "file": None, "type": None}
    if found is not None:
        data, kind = found
        name = f"{slug}.{_TYPES[kind]}"
        (cache / name).write_bytes(data)
        record.update(file=name, type=kind)
    meta_path.write_text(json.dumps(record), encoding="utf-8")
    return found


__all__ = ["KNOWN_SITES", "MAX_BYTES", "TTL_DAYS", "logo", "website"]
