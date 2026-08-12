"""Where the static files live, and how a request path maps onto them.

Resolved from ``Path(__file__).parent``, deliberately not from
``config.install_root()``. That helper returns the *repository root* — correct for
an editable checkout and wrong from site-packages, which is a latent bug
`export.template_path` already carries. Anchoring on this module's own location is
right in both layouts.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path

STATIC_ROOT = Path(__file__).resolve().parent / "static"

_EXTRA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".map": "application/json; charset=utf-8",
}


def content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _EXTRA_TYPES:
        return _EXTRA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def resolve(relative: str) -> Path | None:
    """Map a URL path under /static/ to a file, or None if it escapes the root.

    The containment check is the point. `http.server`'s own translate_path is not
    in play here because routing is manual, so directory traversal has to be
    refused explicitly rather than assumed impossible.
    """
    relative = relative.lstrip("/")
    if not relative:
        return None
    candidate = (STATIC_ROOT / relative).resolve()
    try:
        candidate.relative_to(STATIC_ROOT.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


#: `/static/...` in an href or src, up to the closing quote.
_ASSET_REF = re.compile(r'(?P<attr>href|src)="(?P<path>/static/[^"?#]+)"')

#: `@import url("./css/components-forms.css");`, quoted or not.
_CSS_IMPORT = re.compile(r"""@import\s+url\(\s*(['"]?)(?P<path>[^'")]+)\1\s*\)\s*;""", re.I)

#: A relative `url(...)` inside a stylesheet — not absolute, not a data URI.
_CSS_URL = re.compile(
    r"""url\(\s*(?P<q>['"]?)(?P<path>(?!data:|https?:|//|/)[^'")]+)(?P=q)\s*\)""", re.I
)

#: How deep an `@import` chain may nest before we stop following it. The vendored
#: sheet is one level; anything deeper is a loop or a mistake.
_MAX_IMPORT_DEPTH = 4


def css_parts(path: Path, *, depth: int = 0) -> list[Path]:
    """`path` and every stylesheet it imports, transitively, in load order."""
    out = [path]
    if depth >= _MAX_IMPORT_DEPTH:
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for match in _CSS_IMPORT.finditer(text):
        child = (path.parent / match["path"]).resolve()
        try:
            child.relative_to(STATIC_ROOT.resolve())
        except ValueError:
            continue
        if child.is_file():
            out.extend(css_parts(child, depth=depth + 1))
    return out


def bundle_css(path: Path, *, depth: int = 0) -> str:
    """One stylesheet with its `@import`s inlined and their asset URLs re-anchored.

    **This closes a hole in the versioning below.** `stamp` puts a version token on
    every URL the *page* references, which made `styles.css` uncacheably fresh —
    and did nothing at all for the twelve files `styles.css` itself pulls in with
    `@import`. Those were requested at bare URLs, so a browser or an edge cache
    between the operator and the console could hold one layer from last month
    behind a parent that looked current. The visible symptom is the one that
    started this: the form layer missing while everything else was fine, so every
    dropdown fell back to a native control with the custom chevron still drawn
    beside it, and the switches rendered as bare buttons.

    Inlining also removes twelve serial round-trips — an `@import` is discovered
    only after its parent has been parsed — which is the other way this used to go
    wrong: on a slow link the page painted before the form layer arrived.

    Relative `url(...)` references are rewritten to absolute `/static/...` paths as
    each file is folded in. Without that, `tokens/fonts.css` asking for
    `../../fonts/Inter.woff2` would resolve against the *parent's* directory once
    inlined, and the console would silently lose its fonts.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""

    def absolutise(match: re.Match[str]) -> str:
        target = (path.parent / match["path"]).resolve()
        try:
            relative = target.relative_to(STATIC_ROOT.resolve())
        except ValueError:
            return match[0]
        return f'url("/static/{relative.as_posix()}")'

    def inline(match: re.Match[str]) -> str:
        child = (path.parent / match["path"]).resolve()
        try:
            child.relative_to(STATIC_ROOT.resolve())
        except ValueError:
            return match[0]
        if not child.is_file() or depth >= _MAX_IMPORT_DEPTH:
            # Leave the @import alone rather than dropping the layer: a missing
            # file should fail as a 404 in the network panel, not as a stylesheet
            # that quietly lost a third of its rules.
            return match[0]
        return f"/* {match['path']} */\n{bundle_css(child, depth=depth + 1)}"

    return _CSS_URL.sub(absolutise, _CSS_IMPORT.sub(inline, text))


def version_token(path: Path) -> str:
    """A short token that changes when the file does.

    Modification time and size rather than a content hash: this runs for every
    reference on every page load, and hashing three megabytes of vendored
    JavaScript to discover it has not changed is a poor trade. A touched-but-
    identical file gets a new token, which costs one re-download and is the
    harmless direction to be wrong in.

    A stylesheet's token covers every file it imports, because that is what is
    actually served for it — see :func:`bundle_css`. Editing a layer therefore
    changes the parent's URL, which is the whole mechanism.
    """
    parts = css_parts(path) if path.suffix.lower() == ".css" else [path]
    tokens: list[str] = []
    for part in parts:
        try:
            stat = part.stat()
        except OSError:
            return "0"
        tokens.append(f"{stat.st_mtime_ns:x}-{stat.st_size:x}")
    if len(tokens) == 1:
        return tokens[0]
    digest = hashlib.sha1("|".join(tokens).encode("ascii"), usedforsecurity=False)
    return digest.hexdigest()[:16]


def stamp(html: str) -> str:
    """Rewrite `/static/...` references to carry their file's version.

    **Why this exists.** Static files were served at bare URLs with
    `Cache-Control: no-cache`. That is correct and it is not enough: a browser
    holding `app.js`, or a CDN edge in front of a published console, can go on
    serving last week's front end no matter how many times the server is
    restarted — and the operator has no way to tell, because the page still
    loads. It happened: a rebuilt panel and a rewritten animation both appeared
    to have "no effect" after a restart.

    Versioning the URL removes the question. A changed file is a different URL,
    so nothing anywhere can serve the old bytes; an unchanged file keeps its URL
    and stays cached. `index.html` itself is sent `no-store`, so the new tokens
    always reach the browser.
    """

    def swap(match: re.Match[str]) -> str:
        relative = match["path"][len("/static/") :]
        target = resolve(relative)
        if target is None:
            return match[0]
        return f'{match["attr"]}="{match["path"]}?v={version_token(target)}"'

    return _ASSET_REF.sub(swap, html)


def missing_vendor() -> list[str]:
    """Vendored files the page needs that are not on disk.

    Called at startup so a half-vendored install fails with a list of names rather
    than a blank page and a console full of 404s.
    """
    required = (
        "vendor/react.js",
        "vendor/react-dom.js",
        "vendor/htm.js",
        "vendor/lucide.js",
        "vendor/d3.js",
        "vendor/topojson-client.js",
        "vendor/meridian/styles.css",
        "vendor/meridian/_ds_bundle.js",
        "vendor/dc-map.js",
        "vendor/dc-map3d.js",
    )
    return [name for name in required if not (STATIC_ROOT / name).is_file()]


__all__ = [
    "STATIC_ROOT",
    "content_type",
    "missing_vendor",
    "resolve",
    "stamp",
    "version_token",
]
