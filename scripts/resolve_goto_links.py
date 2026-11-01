"""Turn the reading queue's google.com/goto links back into the pages they point to.

usage (on the production host, where the database is written):
    python scripts/resolve_goto_links.py            # dry run: counts, writes nothing
    python scripts/resolve_goto_links.py --apply

**What happened.** From about 01:30 UTC on 2026-10-30 Google let only the top three
links of a fresh results page be followed, so Serper returned results 4-10 as
`https://www.google.com/goto?url=…` redirects. Google's robots.txt forbids `/goto`,
so the reader refused every one: 829 of the 1,394 URLs found on 10-30 and 668 of the
1,146 found on 10-31, close to 60% of each night's search finds. Serper restored real
links at 17:00 UTC on 10-31, and its free `/resolve` endpoint still turns a stored
goto link into its real URL (tested 11-01 on links six days old).

**What this does.** Sends every queued goto link to `/resolve` (free, its own rate
limit, waiting out any `retryAfter`), then for each one that comes back real:

* the real page is already in the queue, found again since: the goto row is set to
  `skipped`, its error naming the page it resolved to, so it is not tried again;
* otherwise the goto row becomes that page: its URL is replaced, it is set back to
  `discovered` with its attempts, errors and screening cleared, and it keeps its feed,
  title and dates, so the headline screen and the reader treat it like any new find.

A link `/resolve` cannot turn into a real URL is left exactly as it was. Holds the
database's write lock, so it will not run beside the nightly loop or another writer.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tracker.config import get_settings  # noqa: E402
from tracker.db import AlreadyRunning, acquire_write_lock, open_db, session_scope  # noqa: E402
from tracker.models import IngestUrl  # noqa: E402
from tracker.normalize import canonical_url  # noqa: E402
from tracker.vocab import PENDING_URL_STATUS  # noqa: E402

RESOLVE = "https://google.serper.dev/resolve"
GOTO = "google.com/goto"
BATCH = 40
TRIES = 4
RUN_ID = "resolve-goto"


def resolve(client: httpx.Client, links: list[str], key: str) -> dict[str, str]:
    """{goto link: real URL} for the links `/resolve` turned into real ones."""
    body: dict = {"organic": [{"title": "", "link": u} for u in links]}
    for attempt in range(TRIES):
        reply = client.post(RESOLVE, headers={"X-API-KEY": key}, json=body)
        reply.raise_for_status()
        body = reply.json()
        wait = body.get("retryAfter")
        if not wait:
            break
        if attempt < TRIES - 1:
            time.sleep(min(float(wait), 60.0))
    out = {}
    for original, item in zip(links, body.get("organic") or [], strict=False):
        link = str(item.get("link") or "")
        if link.startswith("http") and GOTO not in link:
            out[original] = canonical_url(link)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write; without it, a dry run")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.has_serper_key():
        print("no Serper key in this environment", file=sys.stderr)
        return 2
    key = settings.serper_api_key.get_secret_value()
    db = settings.resolve_db()

    release = None
    if args.apply:
        try:
            release = acquire_write_lock(db, command="resolve goto links")
        except AlreadyRunning as exc:
            print(f"another writer holds the database: {exc}", file=sys.stderr)
            return 1
    engine = open_db(db, readonly=not args.apply)
    try:
        with session_scope(engine, commit=args.apply) as session:
            rows = list(session.scalars(select(IngestUrl).where(IngestUrl.url.like(f"%{GOTO}%"))))
            print(f"{len(rows)} goto link(s) in the queue")
            resolved: dict[str, str] = {}
            with httpx.Client(timeout=120, trust_env=True) as client:
                for start in range(0, len(rows), BATCH):
                    resolved.update(
                        resolve(client, [r.url for r in rows[start : start + BATCH]], key)
                    )
                    print(
                        f"  resolved {len(resolved)} of {min(start + BATCH, len(rows))}", flush=True
                    )

            known = set(
                session.scalars(select(IngestUrl.url).where(IngestUrl.url.not_like(f"%{GOTO}%")))
            )
            requeued = duplicates = unresolved = 0
            for row in rows:
                real = resolved.get(row.url)
                if real is None:
                    unresolved += 1
                    continue
                if real in known:
                    duplicates += 1
                    row.status = "skipped"
                    row.error = f"google goto link; resolved to {real}, already queued"
                    continue
                known.add(real)
                requeued += 1
                row.url = real
                row.status = PENDING_URL_STATUS
                row.run_id = RUN_ID
                row.attempts = 0
                row.failures = 0
                row.error = None
                row.http_status = None
                row.via = None
                row.screened_by = None
                row.read_by = None
            verb = "" if args.apply else "would be "
            print(
                f"{requeued} {verb}queued again as the real page, {duplicates} {verb}retired "
                f"(the page was already queued), {unresolved} left as they were (not resolved)"
            )
            if not args.apply:
                print("dry run: nothing written. Re-run with --apply.")
    finally:
        engine.dispose()
        if release is not None:
            release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
