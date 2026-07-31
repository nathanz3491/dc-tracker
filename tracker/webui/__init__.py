"""The local console: the database as a live page, and the CLI as buttons.

Distinct from `tracker export html`, and both are worth having. The export is one
self-contained file you can email; it is frozen at the moment it was written and
cannot run anything. This is a server on loopback: it reads the database on every
request, and it can execute the commands that change it.

Deliberately built on `http.server`. The dependency list here is
sqlalchemy/pydantic/typer/rich/httpx, and a single-operator console on 127.0.0.1
does not earn an ASGI stack. The front end is React served from vendored files
with no build step, so the repo stays Python-only.
"""

from __future__ import annotations

__all__ = ["serve"]


def serve(*args, **kwargs):
    """Lazy re-export so importing the package does not pull in the whole server."""
    from tracker.webui.server import serve as _serve

    return _serve(*args, **kwargs)
