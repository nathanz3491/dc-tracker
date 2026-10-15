"""The reader documentation: which files make it up, and which addresses serve it.

The docs are pages of Markdown in `tracker/webui/docs/<lang>/<slug>.md`, ordered by
`docs/manifest.json`, and rendered in the browser by `static/docs/docs.js`. This
module never renders anything. It answers two questions for the server: is this
request for the docs, and is this file one of theirs.

**Public, and still two exact lists.** The docs are for anybody, signed in or not,
so they sit outside the sign-in like the front page does — and for the same reason
as `assets.PUBLIC_STATIC` they are an allowlist, not a directory. The files a
stranger may have are the manifest and the one `.md` per page per language that the
manifest names, compared as whole strings before the filesystem is touched. A file
dropped into `docs/` without a manifest entry is not served.

**Two addresses, one site.** On the docs hostname (`TRACKER_DOCS_HOSTNAME`, read
from `.env` so the repo never names it) the whole host is the docs, and nothing of
the console answers there. On any other host the same site is under `/docs/`, which
is how it is previewed on a machine with no tunnel. The page shell is the same
bytes either way, told its base by a `dc-base` meta, and the viewer builds every
address from that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DOCS_ROOT = Path(__file__).resolve().parent / "docs"
MANIFEST = "manifest.json"

#: The languages every page is written in. The manifest must give a title in each,
#: and a page missing in one is a broken page, not a fallback.
LANGUAGES: tuple[str, ...] = ("en", "zh")

#: A page's name in its address and on disk. Lowercase, digits and hyphens, so it
#: can never be read as a path segment or close a string.
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")

#: A hostname as `.env` may give it. Checked once, at startup, because the value
#: reaches a `Location` header.
_HOSTNAME = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


class ManifestError(ValueError):
    """The manifest names something that is not there, or is shaped wrong."""


@dataclass(frozen=True)
class Page:
    slug: str
    section: str
    titles: dict[str, str]


@dataclass(frozen=True)
class Site:
    pages: tuple[Page, ...]

    @property
    def slugs(self) -> frozenset[str]:
        return frozenset(page.slug for page in self.pages)

    @property
    def files(self) -> frozenset[str]:
        """Every content file a stranger may fetch, relative to `DOCS_ROOT`."""
        return frozenset(
            {MANIFEST} | {f"{lang}/{page.slug}.md" for page in self.pages for lang in LANGUAGES}
        )


def _titles(raw: Any, where: str) -> dict[str, str]:
    if not isinstance(raw, dict) or any(not str(raw.get(lang) or "").strip() for lang in LANGUAGES):
        raise ManifestError(f"{where} needs a title in each of {', '.join(LANGUAGES)}")
    return {lang: str(raw[lang]) for lang in LANGUAGES}


def parse(data: Any, root: Path = DOCS_ROOT) -> Site:
    """The site a manifest describes, after checking every page it names exists."""
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise ManifestError("the manifest must be an object with a `sections` list")
    pages: list[Page] = []
    seen: set[str] = set()
    for section in data["sections"]:
        sid = str((section or {}).get("id") or "")
        if not _SLUG.fullmatch(sid):
            raise ManifestError(f"section id {sid!r} is not a slug")
        _titles(section.get("title"), f"section {sid!r}")
        for entry in section.get("pages") or []:
            slug = str((entry or {}).get("slug") or "")
            if not _SLUG.fullmatch(slug):
                raise ManifestError(f"page {slug!r} in {sid!r} is not a slug")
            if slug in seen:
                raise ManifestError(f"page {slug!r} is listed twice")
            seen.add(slug)
            for lang in LANGUAGES:
                if not (root / lang / f"{slug}.md").is_file():
                    raise ManifestError(f"page {slug!r} has no {lang}/{slug}.md")
            pages.append(Page(slug, sid, _titles(entry.get("title"), f"page {slug!r}")))
    if not pages:
        raise ManifestError("the manifest lists no pages")
    return Site(tuple(pages))


@lru_cache(maxsize=4)
def _load(stamp: float) -> Site:
    return parse(json.loads((DOCS_ROOT / MANIFEST).read_text(encoding="utf-8")))


def site() -> Site:
    """The manifest as it is on disk now, re-read whenever the file changes.

    Keyed on the file's mtime so a deploy that adds a page is served without a
    restart: the docs are edited here, pushed, and picked up by the host, and a
    page that needed a restart to appear would break that.
    """
    return _load((DOCS_ROOT / MANIFEST).stat().st_mtime)


def content(relative: str) -> Path | None:
    """The content file `relative` names, if it is one of the site's, else None."""
    if relative not in site().files:
        return None
    path = DOCS_ROOT / relative
    return path if path.is_file() else None


def is_page(lang: str, slug: str) -> bool:
    return lang in LANGUAGES and slug in site().slugs


def hostname(raw: str | None) -> str | None:
    """`TRACKER_DOCS_HOSTNAME`, normalised, or None. Raises on a malformed value."""
    value = (raw or "").strip().lower().rstrip(".")
    if not value:
        return None
    if not _HOSTNAME.fullmatch(value):
        raise ValueError(f"TRACKER_DOCS_HOSTNAME {raw!r} is not a hostname")
    return value


def request_host(header: str | None) -> str:
    """The hostname a request was sent to, without its port, lowercased."""
    value = (header or "").strip().lower()
    if value.startswith("["):  # an IPv6 literal never names the docs
        return value
    return value.split(":", 1)[0].rstrip(".")


__all__ = [
    "DOCS_ROOT",
    "LANGUAGES",
    "ManifestError",
    "Page",
    "Site",
    "content",
    "hostname",
    "is_page",
    "parse",
    "request_host",
    "site",
]
