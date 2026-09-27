"""Where the static files live, how a request path maps onto them, and which of
them a stranger may have.

Resolved from ``Path(__file__).parent``, deliberately not from
``config.install_root()``. That helper returned the *repository root* — correct for
an editable checkout and wrong from site-packages, which is a latent bug
`export.template_path` already carries. Anchoring on this module's own location is
right in both layouts.

**The public surface is two exact lists, not a directory.** Before signing in, a
browser may fetch the pages in `PUBLIC_PAGES` at their own routes and the files in
`PUBLIC_STATIC` under `/static/`, and nothing else. Both are compared as whole
strings before the filesystem is touched: a prefix rule would admit whatever is
added under it later, and on the case-folding filesystems this runs on,
`/static/PUBLIC/site.css` names a real file that no string comparison against
`public/` would have refused. The pages live in `static/public/` beside the two
files they share, but are never served from `/static/public/*.html`.

**Every reference a page makes must be written `href="/static/…"` or
`src="/static/…"`, double quotes, exactly.** That is the only spelling `stamp`
versions; anything else is fetched at a bare URL, answered `no-cache`, and — on a
public page — is not in `PUBLIC_STATIC`, so it 401s for the very visitor the page
is for.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path

STATIC_ROOT = Path(__file__).resolve().parent / "static"

#: The pages and the two files a stranger is shown. See the module docstring.
PUBLIC_ROOT = STATIC_ROOT / "public"

#: Everything under `/static/` an anonymous request may have, relative to
#: `STATIC_ROOT`, compared as exact strings. The fonts are the console's own files
#: at the console's own URLs — Latin subsets only — so a visitor who then signs in
#: already has them cached.
PUBLIC_STATIC: frozenset[str] = frozenset(
    {
        "public/site.css",
        "public/site.js",
        # Instrument Serif 400, latin.
        "vendor/fonts/jizBRFtNs2ka5fXjeivQ4LroWlx-6zUTjg.woff2",
        # Inter 400 to 700, latin.
        "vendor/fonts/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1ZL7.woff2",
        # JetBrains Mono 400 to 600, latin.
        "vendor/fonts/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwg.woff2",
    }
)

#: The public pages, by route (the path with its slashes stripped), to their file
#: under `PUBLIC_ROOT`. `""` is the front page a stranger gets at `/`.
PUBLIC_PAGES: dict[str, str] = {
    "": "home.html",
    "index.html": "home.html",
    "signin": "signin.html",
    "register": "register.html",
    "forgot": "forgot.html",
    "reset": "reset.html",
    "confirm": "confirm.html",
}

#: What a stranger gets, with a 404, for a path that is nothing at all.
NOT_FOUND_PAGE = "notfound.html"

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

#: A relative `url(...)` inside a stylesheet — not absolute, not a data URI, and not
#: a same-document reference such as `url(#hatch)`, which names an element of the
#: page and would otherwise be rewritten into a URL under `/static/`.
_CSS_URL = re.compile(
    r"""url\(\s*(?P<q>['"]?)(?P<path>(?!data:|https?:|//|/|#)[^'")]+)(?P=q)\s*\)""", re.I
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


def _split_suffix(raw: str) -> tuple[str, str]:
    """`(path, fragment)` of a `url()` target: its `?query` dropped, its `#…` kept."""
    fragment = raw[raw.index("#") :] if "#" in raw else ""
    return re.split(r"[?#]", raw, maxsplit=1)[0], fragment


def _css_target(part: Path, raw: str) -> tuple[Path, str] | None:
    """Where one `url()` in `part` points, as `(file, fragment)`, if inside the root."""
    target_path, fragment = _split_suffix(raw)
    if not target_path:
        return None
    target = (part.parent / target_path).resolve()
    try:
        target.relative_to(STATIC_ROOT.resolve())
    except ValueError:
        return None
    return target, fragment


def _css_urls(part: Path) -> list[Path]:
    """The `url()` targets of one stylesheet that fall under `STATIC_ROOT`.

    Whether or not they exist: a missing one is still part of what the stylesheet
    asks for, and `version_token` says so rather than ignoring it.
    """
    try:
        text = part.read_text(encoding="utf-8")
    except OSError:
        return []
    found = (_css_target(part, match["path"]) for match in _CSS_URL.finditer(text))
    return [hit[0] for hit in found if hit is not None]


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

    **Each one also carries its file's version**, for the reason `stamp` exists.
    The fonts used to be fetched at bare URLs answered `no-cache` with no
    validator, so every page load downloaded them again. A `?query` in the source
    is replaced by the token; a `#fragment` is kept. A target that is not on disk
    is left unversioned, to fail as a plain 404.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""

    def absolutise(match: re.Match[str]) -> str:
        hit = _css_target(path, match["path"])
        if hit is None:
            return match[0]
        target, fragment = hit
        relative = target.relative_to(STATIC_ROOT.resolve()).as_posix()
        version = f"?v={version_token(target)}" if target.is_file() else ""
        return f'url("/static/{relative}{version}{fragment}")'

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
    actually served for it — see :func:`bundle_css` — and the token of every file
    its `url()`s name, because the served text carries those tokens. Editing a
    layer, or replacing a font, therefore changes the parent's URL, which is the
    whole mechanism.

    **Only the requested file's own stat failure returns "0".** A `url()` target
    that is missing contributes `<rel>:missing` instead. It used to return "0" for
    the whole stylesheet, and a request that asked with that "0" matched it and was
    cached for a year — one bad font path froze every public stylesheet in place.
    """
    own = _stat_token(path)
    if own is None:
        return "0"
    if path.suffix.lower() != ".css":
        return own
    root = STATIC_ROOT.resolve()

    def named(target: Path) -> str:
        found = _stat_token(target)
        return f"{target.relative_to(root).as_posix()}:{found or 'missing'}"

    tokens: list[str] = []
    for index, part in enumerate(css_parts(path)):
        # An imported layer is named, so one that vanishes mid-request is a change
        # to this token rather than a reason to give up on it.
        tokens.append(own if index == 0 else named(part))
        tokens.extend(named(target) for target in _css_urls(part))
    if len(tokens) == 1:
        return tokens[0]
    digest = hashlib.sha1("|".join(tokens).encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()[:16]


def _stat_token(path: Path) -> str | None:
    """`mtime-size` in hex, or None when the file cannot be stat'ed."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"


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

    **Only `href="/static/…"` and `src="/static/…"` are recognised** — double
    quotes, the attribute name in lower case, no query or fragment already on the
    path. A reference written any other way is left as it is and fetched
    unversioned, so every page, the public ones included, uses that spelling.
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


def missing_public() -> list[str]:
    """Public files and pages that are not on disk, relative to `STATIC_ROOT`.

    Checked at startup for the reason `missing_vendor` is: without them every
    signed-out visit is a 500, which a published console would show to everybody.
    """
    pages = sorted({*PUBLIC_PAGES.values(), NOT_FOUND_PAGE})
    missing = [name for name in sorted(PUBLIC_STATIC) if not (STATIC_ROOT / name).is_file()]
    missing += [f"public/{name}" for name in pages if not (PUBLIC_ROOT / name).is_file()]
    return missing


__all__ = [
    "NOT_FOUND_PAGE",
    "PUBLIC_PAGES",
    "PUBLIC_ROOT",
    "PUBLIC_STATIC",
    "STATIC_ROOT",
    "bundle_css",
    "content_type",
    "css_parts",
    "missing_public",
    "missing_vendor",
    "resolve",
    "stamp",
    "version_token",
]
