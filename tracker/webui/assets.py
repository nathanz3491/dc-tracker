"""Where the static files live, and how a request path maps onto them.

Resolved from ``Path(__file__).parent``, deliberately not from
``config.install_root()``. That helper returns the *repository root* — correct for
an editable checkout and wrong from site-packages, which is a latent bug
`export.template_path` already carries. Anchoring on this module's own location is
right in both layouts.
"""

from __future__ import annotations

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


def version_token(path: Path) -> str:
    """A short token that changes when the file does.

    Modification time and size rather than a content hash: this runs for every
    reference on every page load, and hashing three megabytes of vendored
    JavaScript to discover it has not changed is a poor trade. A touched-but-
    identical file gets a new token, which costs one re-download and is the
    harmless direction to be wrong in.
    """
    try:
        stat = path.stat()
    except OSError:
        return "0"
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
        "vendor/dc-campus.js",
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
