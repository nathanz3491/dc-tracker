"""Where the static files live, and how a request path maps onto them.

Resolved from ``Path(__file__).parent``, deliberately not from
``config.install_root()``. That helper returns the *repository root* — correct for
an editable checkout and wrong from site-packages, which is a latent bug
`export.template_path` already carries. Anchoring on this module's own location is
right in both layouts.
"""

from __future__ import annotations

import mimetypes
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


__all__ = ["STATIC_ROOT", "content_type", "missing_vendor", "resolve"]
