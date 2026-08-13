#!/usr/bin/env python
"""Size the stage 1-3 defects on the live database. Reads only, spends nothing.

Companion to scripts/measure_extraction.py, which already covers the evidence
census, prompt vintages and the *observable* recency inversions. This one sizes
the populations those sections cannot see:

1. Tiebreak exposure  - stored values decided by crawl order because the claims
                        tied on credibility. measure_extraction.py's section 3
                        only counts the ones where BOTH sides happen to carry a
                        published_at, which is 11.8% of sources, so it reports a
                        floor of a floor.
2. Generation blocks  - capacity_block rows that name a power asset.
3. Block key forks    - one tranche holding two keys because one source named
                        the campus and another did not.
4. Event duplication  - the same milestone under several dates.
5. Source yield       - how many citations add nothing to a project.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import select  # noqa: E402

from tracker.db import open_db, session_scope  # noqa: E402
from tracker.models import CapacityBlock, Event, Project, Source  # noqa: E402
from tracker.sources import decisive_by_source  # noqa: E402
from tracker.upsert import claims_by_field  # noqa: E402
from tracker.vocab import TRACKED_FIELDS  # noqa: E402


def rule(t: str) -> None:
    print(f"\n{'=' * 74}\n{t}\n{'=' * 74}")


# --- 1. tiebreak exposure ---------------------------------------------------


def tiebreak_exposure(session) -> None:
    rule("1. Tiebreak exposure - values decided by crawl order, not by evidence")
    projects = session.scalars(select(Project)).all()
    total = 0
    dated_both = 0
    per_field: dict[str, int] = defaultdict(int)
    examples: list[str] = []

    for p in projects:
        sources = session.scalars(select(Source).where(Source.project_id == p.id)).all()
        if len(sources) < 2:
            continue
        for field, claims in claims_by_field(sources).items():
            if field not in TRACKED_FIELDS or len(claims) < 2:
                continue
            top = claims[0]
            # Rivals that tie on everything the sort considers *above* recency.
            rivals = [
                c
                for c in claims[1:]
                if c.confirmed == top.confirmed and c.weight == top.weight and c.value != top.value
            ]
            if not rivals:
                continue
            total += 1
            per_field[field] += 1
            if top.published_at and any(r.published_at for r in rivals):
                dated_both += 1
            if len(examples) < 12:
                r = rivals[0]
                examples.append(
                    f"  #{p.id:<4} {field:<16} kept {str(top.value)[:18]:<18} "
                    f"over {str(r.value)[:18]:<18} "
                    f"(pub {str(top.published_at)[:10] or '-':<10} vs "
                    f"{str(r.published_at)[:10] or '-'})"
                )

    print(f"{total} stored value(s) had a same-weight, same-confirmation rival that")
    print("disagreed. The winner was chosen by `fetched_at` - when the crawler")
    print("happened to visit. Every one of these is a coin flip today.\n")
    for f, n in sorted(per_field.items(), key=lambda kv: -kv[1]):
        print(f"  {f:<18} {n}")
    print(f"\n  of those, both sides carry a published_at: {dated_both}")
    print(f"  => visible to measure_extraction.py section 3: {dated_both}")
    print(f"  => invisible, decided by crawl order anyway:  {total - dated_both}")
    print("\nexamples:")
    for e in examples:
        print(e)


# --- 2. generation blocks ---------------------------------------------------

_GEN = re.compile(
    r"\b(gas|turbine|ccgt|combined[- ]cycle|solar|wind|battery|bess|nuclear|smr|"
    r"substation|transmission|switchyard|generating|generation|power plant|"
    r"powerplant|units?[- ]\d|farms?)\b",
    re.I,
)


def generation_blocks(session) -> None:
    rule("2. Generation and grid assets stored as data-centre capacity blocks")
    blocks = session.scalars(select(CapacityBlock)).all()
    hits = [b for b in blocks if _GEN.search(f"{b.label} {b.parent or ''}")]
    mw = sum(b.mw or 0 for b in hits)
    print(
        f"{len(hits)} of {len(blocks)} capacity_block rows name a power asset "
        f"({100 * len(hits) / max(1, len(blocks)):.0f}%)"
    )
    print(f"they carry {mw:,.0f} MW that is generation, not IT load\n")
    by_project: dict[int, list] = defaultdict(list)
    for b in hits:
        by_project[b.project_id].append(b)
    for pid, bs in sorted(by_project.items(), key=lambda kv: -sum(b.mw or 0 for b in kv[1]))[:10]:
        s = sum(b.mw or 0 for b in bs)
        print(
            f"  #{pid:<4} {len(bs)} block(s), {s:>8,.0f} MW  "
            + ", ".join(f"{b.label}" for b in bs[:4])
        )


# --- 3. block key forks -----------------------------------------------------


def key_forks(session) -> None:
    rule("3. One tranche, two keys - the generic/parented fork")
    blocks = session.scalars(select(CapacityBlock)).all()
    by_project: dict[int, list] = defaultdict(list)
    for b in blocks:
        by_project[b.project_id].append(b)

    forks = 0
    collide = 0
    rows: list[str] = []
    for pid, bs in by_project.items():
        keys = {b.block_key: b for b in bs}
        for key, b in keys.items():
            if "." not in key:
                continue
            # a parented key whose tail also exists on its own
            for part in key.split("."):
                tail = part
                if tail in keys and tail != key:
                    other = keys[tail]
                    forks += 1
                    a, c = b.mw, other.mw
                    verdict = "COLLIDES" if (a and c and a != c) else "mergeable"
                    if verdict == "COLLIDES":
                        collide += 1
                    if len(rows) < 14:
                        rows.append(
                            f"  #{pid:<4} {key:<26} {a or '-'!s:>8} MW  vs  "
                            f"{tail:<14} {c or '-'!s:>8} MW   {verdict}"
                        )
    print(f"{forks} key pair(s) are the same designator with and without a campus prefix")
    print(f"  {collide} of them disagree on MW -> a merge must refuse and flag")
    print(f"  {forks - collide} agree or one side is null -> a deterministic merge is safe\n")
    for r in rows:
        print(r)


# --- 4. event duplication ---------------------------------------------------


def event_dupes(session) -> None:
    rule("4. One milestone, several dates")
    events = session.scalars(select(Event)).all()
    groups: dict[tuple, list] = defaultdict(list)
    for e in events:
        groups[(e.project_id, e.event_type)].append(e)
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    extra = sum(len(v) - 1 for v in multi.values())
    print(f"{len(events)} events in {len(groups)} (project, type) groups")
    print(f"{len(multi)} group(s) hold more than one date; {extra} rows are the surplus")
    print(
        f"  => {100 * extra / max(1, len(events)):.0f}% of the event table is one "
        f"milestone reported by several outlets\n"
    )
    worst = sorted(multi.items(), key=lambda kv: -len(kv[1]))[:8]
    for (pid, et), evs in worst:
        ds = sorted(str(e.event_date)[:10] for e in evs)
        print(f"  #{pid:<4} {et:<26} {len(evs):>2} rows  {ds[0]} .. {ds[-1]}")


# --- 5. source yield --------------------------------------------------------


def source_yield(session) -> None:
    rule("5. What a marginal citation adds")
    projects = session.scalars(select(Project)).all()
    counts = sorted((len(p.sources), p.id) for p in projects if p.sources)
    if not counts:
        return
    n = len(counts)
    print(f"{n} projects with at least one source")
    print(f"  median sources/project : {counts[n // 2][0]}")
    print(
        "  top 5                  : " + ", ".join(f"#{pid}={c}" for c, pid in reversed(counts[-5:]))
    )

    # How many sources on the heaviest projects decide anything?
    #
    # Delegated to `tracker.sources.decisive_by_source` rather than reimplemented.
    # The first version of this section credited `claims[0]` — the head of the
    # sorted claim list — which is wrong on four of the twelve tracked fields:
    # `mw_built` takes the MAX, `first_announced` the MIN, `phase` the furthest
    # rung, so the strongest source routinely loses those to a weaker one. Sharing
    # the function is what makes "the report agrees with the command" structural
    # instead of a coincidence that survives until either side is edited.
    print()
    for c, pid in reversed(counts[-5:]):
        sources = session.scalars(select(Source).where(Source.project_id == pid)).all()
        attribution = decisive_by_source(list(sources))
        n = len(attribution.won)
        print(
            f"  #{pid:<4} {c:>3} sources, {n:>3} of them decide at "
            f"least one of the 8 scored fields  ({100 * n / c:.0f}%)"
        )
    print("\n  Identity fields (name/company/city/state) are excluded: they are")
    print("  FILL_ONLY, so a 'win' on one records which crawl arrived first.")


def main(argv: list[str] | None = None) -> int:
    # Same reasoning as measure_extraction.py's `main`: print rather than Rich, so
    # nothing negotiates the console encoding on a legacy Windows codepage.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # `--root` mirrors measure_extraction.py, and it is not optional polish here:
    # the database belongs to an *install*, not a checkout, so running this from a
    # git worktree looks for a `data/` that has never existed.
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[argv.index("--root") + 1]).resolve() if "--root" in argv else REPO
    db = (
        Path(argv[argv.index("--db") + 1]).resolve()
        if "--db" in argv
        else root / "data" / "tracker.db"
    )
    if not db.exists():
        sys.exit(f"no database at {db}\nPass --root <install> or --db <file>.")
    engine = open_db(db, readonly=True)
    with session_scope(engine, commit=False) as session:
        print(f"database: {db}")
        tiebreak_exposure(session)
        generation_blocks(session)
        key_forks(session)
        event_dupes(session)
        source_yield(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
