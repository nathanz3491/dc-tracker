#!/usr/bin/env python
"""Would re-reading everything with today's instructions be worth ~2,000 calls?

**This is a measurement, not a migration.** Nothing is written. It re-reads a
random sample of already-cached articles with the current prompt, compares what
comes back against what the stored citation claims, and reports what the full
re-read would actually buy.

The question is real and the answer is not obvious. Zero of 2,574 stored sources
carry the current prompt's stamp and 93% are two versions back, which sounds
alarming and might mean nothing: a prompt edit that tightened one field's wording
changes almost no stored value. Paying ~2,000 LLM calls to find that out is the
expensive way to learn it. Forty is the cheap way.

**The stop rule, written before it runs.** On #10's sources: the generation rows
stay out of the campus tranches, `mw_planned` stays 5,000, `phase` stays
`construction`, no value that has a quote today loses it, and no project is
created. Any failure and the wider re-read does not happen.

    python scripts/measure_reextraction.py --root <install> --sample 40

Costs one LLM call per sampled article. Reads from the article cache only — an
article that is not cached is skipped rather than fetched, so this cannot turn
into a crawl by accident.
"""

from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import select  # noqa: E402

from tracker.config import get_settings  # noqa: E402
from tracker.db import open_db, session_scope  # noqa: E402
from tracker.ingest.crawl import extract_one  # noqa: E402
from tracker.ingest.fetch import FetchResult, cache_path  # noqa: E402
from tracker.llm import default_extractor  # noqa: E402
from tracker.models import Project, Source  # noqa: E402
from tracker.prompts import load_prompt  # noqa: E402
from tracker.vocab import TRACKED_FIELDS  # noqa: E402

#: Fixed, so two runs over the same database sample the same articles and the
#: second one can be compared with the first. A measurement whose worklist moves
#: is a measurement of the worklist.
SEED = 20260813


def rule(t: str) -> None:
    print(f"\n{'=' * 74}\n{t}\n{'=' * 74}")


@dataclass
class Tally:
    read: int = 0
    failed: int = 0
    #: Values the re-read states and the stored citation does not.
    gained: int = 0
    #: Values the stored citation states and the re-read does not.
    lost: int = 0
    #: Values both state, differently.
    changed: int = 0
    #: Values both state identically. The case that means the re-read bought
    #: nothing, and the one the headline number is a fraction of.
    same: int = 0
    #: 待确认 today, quote-backed after. The clearest kind of improvement.
    confirmed_now: int = 0
    #: Quote-backed today, 待确认 after. The clearest kind of regression, and the
    #: reason the report cannot just count "changes".
    unconfirmed_now: int = 0
    #: Projects the re-read names that this database does not track. Counted
    #: because `existing_only` is what stops them becoming rows, and an operator
    #: should see what was refused rather than trust that nothing was.
    unmatched: set[str] = field(default_factory=set)
    per_field: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    examples: list[str] = field(default_factory=list)

    @property
    def moved(self) -> int:
        return self.gained + self.lost + self.changed


def _stored(source: Source) -> tuple[dict, set[str]]:
    """What this citation claims, and which of those are quote-backed."""
    try:
        claims = json.loads(source.claims or "{}")
    except (TypeError, ValueError):
        claims = {}
    confirmed = {f.strip() for f in (source.fields or "").split(",") if f.strip()}
    return (claims if isinstance(claims, dict) else {}), confirmed


def _known(session) -> dict[str, int]:
    """dedup key -> project id, so an unmatched extraction can be named."""
    from tracker.dedup import dedup_key

    out = {}
    for p in session.scalars(select(Project)).all():
        out[dedup_key(p.company, p.city, p.county, p.state)] = p.id
    return out


def sample(session, *, size: int, cache_dir: Path) -> list[tuple[str, list[Source]]]:
    """Cached crawl articles, grouped by URL, in a fixed random order."""
    grouped: dict[str, list[Source]] = defaultdict(list)
    for source in session.scalars(select(Source)).all():
        if (source.extractor or "").startswith("crawl:") and cache_path(
            source.url, cache_dir
        ).exists():
            grouped[source.url].append(source)
    urls = sorted(grouped)
    random.Random(SEED).shuffle(urls)
    return [(url, grouped[url]) for url in urls[:size]]


def compare(session, picks, *, cache_dir: Path, extractor, prompt) -> Tally:
    from tracker.dedup import dedup_key

    tally = Tally()
    known = _known(session)

    for url, sources in picks:
        text = cache_path(url, cache_dir).read_text(encoding="utf-8", errors="replace")
        outcome = extract_one(
            FetchResult(url=url, ok=True, markdown=text),
            prompt=prompt,
            extractor=extractor,
            settings=get_settings(),
        )
        tally.read += 1
        if outcome.status not in ("ok", "no_project"):
            tally.failed += 1
            continue

        for record in outcome.records:
            payload = record.project
            key = dedup_key(
                payload.get("company"),
                payload.get("city"),
                payload.get("county"),
                payload.get("state"),
            )
            if key not in known:
                # What `existing_only` will refuse. Named rather than counted, so
                # a real new campus can be added deliberately later.
                tally.unmatched.add(f"{payload.get('company')} / {payload.get('name')}")
                continue
            row = next((s for s in sources if s.project_id == known[key]), None)
            if row is None:
                continue
            was, was_confirmed = _stored(row)
            fresh = dict(record.sources[0].tracked_claims()) if record.sources else {}
            now_confirmed = set(record.sources[0].confirmed_claims()) if record.sources else set()

            for name in TRACKED_FIELDS:
                before, after = was.get(name), fresh.get(name)
                if before is None and after is None:
                    continue
                if before is None:
                    tally.gained += 1
                elif after is None:
                    tally.lost += 1
                elif str(before).strip().lower() != str(after).strip().lower():
                    tally.changed += 1
                    if len(tally.examples) < 20:
                        tally.examples.append(f"  {name}: {before} -> {after}   {url[:60]}")
                else:
                    tally.same += 1
                    continue
                tally.per_field[name] += 1

            for name in set(was) | set(fresh):
                if name in was_confirmed and name not in now_confirmed and name in fresh:
                    tally.unconfirmed_now += 1
                elif name not in was_confirmed and name in now_confirmed:
                    tally.confirmed_now += 1
    return tally


def report(tally: Tally, *, size: int) -> None:
    rule(f"Re-read {tally.read} cached article(s) with the current prompt")
    rows = [
        ("articles read", tally.read),
        ("could not parse", tally.failed),
        ("values unchanged", tally.same),
        ("values changed", tally.changed),
        ("values gained", tally.gained),
        ("values lost", tally.lost),
        ("now quote-backed", tally.confirmed_now),
        ("no longer quote-backed", tally.unconfirmed_now),
        ("projects not tracked here", len(tally.unmatched)),
    ]
    for label, count in rows:
        print(f"  {label:<28} {count}")

    if tally.per_field:
        print("\n  by field")
        for name, count in sorted(tally.per_field.items(), key=lambda kv: -kv[1]):
            print(f"    {name:<20} {count}")
    if tally.examples:
        print("\n  what moved")
        for line in tally.examples:
            print(line)
    if tally.unmatched:
        print("\n  named but not tracked here (refused by `existing_only`)")
        for name in sorted(tally.unmatched)[:15]:
            print(f"    {name}")

    rule("Verdict")
    if not tally.read:
        print("  nothing cached to read.")
        return
    per_article = tally.moved / tally.read
    total_articles = 1_928
    print(f"  {per_article:.1f} value(s) moved per article read.")
    print(
        f"  Over ~{total_articles:,} cached articles that is ~{per_article * total_articles:,.0f}"
    )
    print(f"  value(s) moved for ~{total_articles:,} LLM calls.")
    print()
    if tally.unconfirmed_now > tally.confirmed_now:
        print("  MORE values LOST their quote than gained one. The wider re-read is a")
        print("  regression at this sample size — do not run it.")
    elif per_article < 0.5:
        print("  Below half a value per article. The stored rows are mostly what the")
        print("  current prompt would produce anyway; the calls buy little.")
    else:
        print("  Worth the calls, on this sample. Re-read with `existing_only` on and")
        print("  check the reference case (#10) before and after.")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[argv.index("--root") + 1]).resolve() if "--root" in argv else REPO
    db = (
        Path(argv[argv.index("--db") + 1]).resolve()
        if "--db" in argv
        else root / "data" / "tracker.db"
    )
    size = int(argv[argv.index("--sample") + 1]) if "--sample" in argv else 40
    if not db.exists():
        sys.exit(f"no database at {db}\nPass --root <install> or --db <file>.")

    cache_dir = REPO / ".cache" / "articles"
    settings = get_settings()
    if not settings.has_api_key():
        sys.exit(
            "TRACKER_DEEPSEEK_API_KEY is not set. This script spends one LLM call "
            "per sampled article and cannot run without it."
        )

    engine = open_db(db, readonly=True)
    prompt = load_prompt("extract-v1")
    extractor = default_extractor(settings)
    with session_scope(engine, commit=False) as session:
        print(f"database: {db}")
        print(f"prompt:   {prompt.stamp}")
        picks = sample(session, size=size, cache_dir=cache_dir)
        if not picks:
            sys.exit(f"no cached crawl articles under {cache_dir}")
        print(f"sample:   {len(picks)} of the cached crawl articles (seed {SEED})")
        tally = compare(session, picks, cache_dir=cache_dir, extractor=extractor, prompt=prompt)
    report(tally, size=size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
