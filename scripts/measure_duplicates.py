"""What the duplicates report holds, and what `resolve --merge` could act on.

Two questions this answers that no command does, both free — no model call, no
network, a read-only handle:

1. **How the suspected pairs break down by evidence class.** `tracker duplicates`
   prints groups and their strongest signal; this prints every class combination,
   which is what tells you whether a change to detection found new duplicates or
   merely relabelled the ones already there.

2. **Which pairs the rails would let a model merge.** `duplicates resolve` answers
   that only by spending a reasoning call per pair — and `--dry-run` spends them
   too, because "what would this run change" cannot be answered without asking.
   The rails, though, do not depend on the model's *answer*, only on its verdict
   and confidence. So this asks `dupresolve.merge_blocked` with a hypothetical
   confident "same" and reports what would survive. That is the whole question
   "how much of the backlog can be settled unattended", answered for nothing.

   What it deliberately does not tell you is whether those merges are *right*.
   Only reading the rows does that.

Usage:

    python scripts/measure_duplicates.py [--db PATH] [--json]

`--json` writes the pair list as one object per line, for diffing two revisions:

    python scripts/measure_duplicates.py --json > before.jsonl
    # change the code
    python scripts/measure_duplicates.py --json > after.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.capex import (
    double_counted_mw,
    duplicate_groups,
    suspected_duplicates,
)
from tracker.db import open_db, session_scope
from tracker.dupresolve import Judgement, merge_blocked
from tracker.models import Project

#: A verdict the model never gave. Confidence above `MERGE_CONFIDENCE` on purpose:
#: the question is what the *rails* refuse, so the confidence floor is taken as
#: satisfied and every other refusal is the answer.
ASSUMED = Judgement("same", 0.95, "simulated: what would the rails allow")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/tracker.db", help="database file")
    ap.add_argument("--json", action="store_true", help="one JSON object per pair, for diffing")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.is_file():
        print(f"database not found: {path}", file=sys.stderr)
        return 2

    engine = open_db(path, readonly=True)
    with session_scope(engine, commit=False) as session:
        pairs = suspected_duplicates(session)
        groups = duplicate_groups(pairs)
        wasted = double_counted_mw(pairs)

        rows: list[dict[str, object]] = []
        for pair in pairs:
            a = session.get(Project, pair.a_id)
            b = session.get(Project, pair.b_id)
            blocked = merge_blocked(pair, ASSUMED, a, b) if a and b else "row missing"
            rows.append(
                {
                    "a_id": pair.a_id,
                    "b_id": pair.b_id,
                    "kinds": list(pair.kinds),
                    "why": pair.why,
                    "a": f"{pair.a_company} — {pair.a_name}",
                    "b": f"{pair.b_company} — {pair.b_name}",
                    "locality": f"{pair.locality}, {pair.state}",
                    "mergeable": blocked is None,
                    "blocked": blocked,
                }
            )

    if args.json:
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        return 0

    print(f"{len(groups)} group(s), {len(pairs)} pair(s), {wasted:,.0f} MW stored twice\n")

    print("by evidence class")
    for combo, count in collections.Counter("+".join(r["kinds"]) for r in rows).most_common():
        print(f"  {combo:<32} {count:>3}")

    mergeable = [r for r in rows if r["mergeable"]]
    print(f"\nwhat the rails would allow at 0.95 confidence: {len(mergeable)}/{len(rows)}")
    for row in mergeable:
        print(f"  #{row['a_id']} + #{row['b_id']}  [{'+'.join(row['kinds'])}]  {row['why']}")
        print(f"      {row['a']}")
        print(f"      {row['b']}")

    print("\nwhy the rest are refused")
    for reason, count in collections.Counter(
        str(r["blocked"]).split(";")[0] for r in rows if not r["mergeable"]
    ).most_common():
        print(f"  {count:>3}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
