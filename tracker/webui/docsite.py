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

import datetime as dt
import json
import re
import shutil
import subprocess
from collections.abc import Mapping
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


# --- the changelog --------------------------------------------------------------------
#
# The release notes end with the project's own `CHANGELOG.md`: every change as its
# developers record it. That file is over half a megabyte and grows every day, and a
# reader opening the release notes must not download it. So the page first loads an
# index — each change's title, heading group and the day it was written — and draws
# the history from it: a header per month, a week at a time, Added / Changed / Fixed
# within each week. A change's text is fetched only when the reader opens it, in
# parts: consecutive entries cut so each part stays under `PART_BYTES` and
# `PART_ENTRIES`, so opening one change fetches at most one part.
#
# The day is when the entry's first line was committed, read with `git blame` from
# the checkout the install runs in: the changelog's headings carry no dates, and the
# file is written newest first under one `[Unreleased]`. An install without git or
# without the file's history gets the changes undated, listed by heading.
#
# Read from the file the install ships beside the package and re-read when its mtime
# moves, like the manifest: the deployed checkout is the source, and a changelog
# that needed a restart to update would be stale after every deploy. An install with
# no changelog (a wheel) says it has none rather than failing the page.

CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"

#: A part's ceiling. Entries are added until the next would pass either, so a part is
#: about a long page of prose; one entry longer than that is a part on its own.
PART_BYTES = 32_000
PART_ENTRIES = 15

#: How much of an entry's title the index carries.
_TITLE_CHARS = 160

_RELEASE = re.compile(r"^##\s+\[?([^\]]+?)\]?\s*$")
_GROUP = re.compile(r"^###\s+(.+?)\s*$")
_ENTRY = re.compile(r"^[-*]\s")
_FENCE = re.compile(r"^\s*```")
_BOLD_TITLE = re.compile(r"\*\*(.+?)\*\*")


def entry_title(entry: str) -> str:
    """An entry's title, as plain text: its opening bold phrase, or its first words.

    Read from the entry's first paragraph, not its first line: a title in bold
    often wraps onto the next line of the file.
    """
    head = re.sub(r"\s+", " ", entry.split("\n\n", 1)[0])
    bold = _BOLD_TITLE.search(head)
    title = bold.group(1) if bold else re.sub(r"^[-*]\s+", "", head)
    title = re.sub(r"[`*_]", "", title).strip()
    return title if len(title) <= _TITLE_CHARS else title[: _TITLE_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True)
class ChangelogPart:
    """Some consecutive entries of one heading group (`### Fixed`) of one release.

    `lines` is where each entry starts in the file, 1-based, which is what dates it.
    """

    release: str
    group: str
    part: int
    parts: int
    entries: tuple[str, ...]
    lines: tuple[int, ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(self.entries) + "\n"

    @property
    def first(self) -> str:
        return entry_title(self.entries[0]) if self.entries else ""


@dataclass(frozen=True)
class Changelog:
    intro: str
    parts: tuple[ChangelogPart, ...]
    #: Line -> the day it was committed (`YYYY-MM-DD`), or None when undated.
    dates: Mapping[int, str] | None = None

    def index(self) -> dict[str, Any]:
        """What the release notes page loads first: every change's title, never its text."""
        dates = self.dates or {}
        items = [
            {
                "title": entry_title(text),
                "group": part.group,
                "date": dates.get(line),
                "part": pid,
                "at": at,
            }
            for pid, part in enumerate(self.parts)
            for at, (text, line) in enumerate(
                zip(part.entries, part.lines or (0,) * len(part.entries), strict=True)
            )
        ]
        return {
            "intro": self.intro,
            "entries": len(items),
            "bytes": sum(len(p.text.encode("utf-8")) for p in self.parts),
            "dated": bool(self.dates),
            "parts": [
                {"id": i, "entries": len(p.entries), "bytes": len(p.text.encode("utf-8"))}
                for i, p in enumerate(self.parts)
            ],
            "items": items,
        }


def _cut(entries: list[tuple[str, int]]) -> list[list[tuple[str, int]]]:
    """Entries grouped into parts under `PART_BYTES` and `PART_ENTRIES`, in order."""
    out: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    size = 0
    for entry in entries:
        cost = len(entry[0].encode("utf-8")) + 2
        if current and (size + cost > PART_BYTES or len(current) >= PART_ENTRIES):
            out.append(current)
            current, size = [], 0
        current.append(entry)
        size += cost
    if current:
        out.append(current)
    return out


def parse_changelog(text: str, dates: Mapping[int, str] | None = None) -> Changelog:
    """A Keep a Changelog file as parts: `## [release]`, `### Group`, `- entries`.

    An entry is a line starting `- ` at the margin and everything indented or blank
    after it, fenced code included, up to the next entry or heading. Prose under a
    release before its first group is the intro the page shows above the changes.
    `dates` maps a line number to the day it was written (`commit_dates`).
    """
    release = ""
    group = ""
    intro: list[str] = []
    runs: list[tuple[str, str, list[tuple[str, int]]]] = []
    entry: list[str] | None = None
    start = 0
    fenced = False

    def close() -> None:
        nonlocal entry
        if entry is not None:
            body = "\n".join(entry).rstrip()
            if body and runs:
                runs[-1][2].append((body, start))
            entry = None

    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        if fenced:
            if entry is not None:
                entry.append(line)
            if _FENCE.match(line):
                fenced = False
            continue
        if line.startswith("# "):
            continue
        heading = _RELEASE.match(line)
        if heading:
            close()
            release, group = heading.group(1), ""
            continue
        heading = _GROUP.match(line)
        if heading:
            close()
            group = heading.group(1)
            runs.append((release, group, []))
            continue
        if _FENCE.match(line):
            fenced = True
        if group and _ENTRY.match(line):
            close()
            entry, start = [line], number
        elif entry is not None:
            entry.append(line)
        elif release and not group and line.strip():
            intro.append(line.strip())
        elif group and line.strip():
            # Prose under a group before its first entry: kept, as an entry of its own.
            entry, start = [line], number
    close()

    parts: list[ChangelogPart] = []
    for run_release, run_group, entries in runs:
        chunks = _cut(entries)
        for n, chunk in enumerate(chunks, start=1):
            parts.append(
                ChangelogPart(
                    run_release or "Unreleased",
                    run_group,
                    n,
                    len(chunks),
                    tuple(body for body, _ in chunk),
                    tuple(line for _, line in chunk),
                )
            )
    return Changelog(" ".join(intro), tuple(parts), dates or None)


def _local_day(when: int, tz: str) -> str:
    """A commit time as the day it was in its author's own time zone."""
    sign = -1 if tz.startswith("-") else 1
    digits = tz.lstrip("+-").rjust(4, "0")
    offset = dt.timedelta(minutes=sign * (int(digits[:2]) * 60 + int(digits[2:4])))
    return (dt.datetime.fromtimestamp(when, dt.UTC) + offset).date().isoformat()


def commit_dates(path: Path) -> dict[int, str] | None:
    """Line number -> the day that line was committed, from `git blame`; or None.

    The author's day in their own time zone, so a change written at 01:00 in Beijing
    is dated that morning rather than the evening before in UTC. Lines not yet
    committed are dated today, which is when a deploy would ship them. None when git
    or the file's history is not there — a wheel, a tarball — and the page lists the
    changes undated.
    """
    git = shutil.which("git") or "/usr/bin/git"
    try:
        result = subprocess.run(
            [git, "blame", "--line-porcelain", "--", path.name],
            cwd=path.parent,
            capture_output=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    dates: dict[int, str] = {}
    line = 0
    when, tz = 0, "+0000"
    for raw in result.stdout.decode("utf-8", "replace").split("\n"):
        if raw.startswith("\t"):
            line += 1
            dates[line] = _local_day(when, tz)
        elif raw.startswith("author-time "):
            when = int(raw[len("author-time ") :])
        elif raw.startswith("author-tz "):
            tz = raw[len("author-tz ") :]
    return dates or None


@lru_cache(maxsize=2)
def _changelog(stamp: float) -> Changelog:
    return parse_changelog(CHANGELOG.read_text(encoding="utf-8"), commit_dates(CHANGELOG))


def changelog() -> Changelog | None:
    """The changelog as it is on disk now, or None when this install has none."""
    try:
        return _changelog(CHANGELOG.stat().st_mtime)
    except FileNotFoundError:
        return None


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
    "CHANGELOG",
    "DOCS_ROOT",
    "LANGUAGES",
    "PART_BYTES",
    "PART_ENTRIES",
    "Changelog",
    "ChangelogPart",
    "ManifestError",
    "Page",
    "Site",
    "changelog",
    "commit_dates",
    "content",
    "entry_title",
    "hostname",
    "is_page",
    "parse",
    "parse_changelog",
    "request_host",
    "site",
]
