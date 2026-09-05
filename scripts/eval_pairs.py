"""Score duplicate detection against the answers somebody already gave.

**The labels have been in the database all along.** Every pair an operator parked
is a recorded "these are different sites"; every `project_alias` row is a recorded
"this identity is that row". Nothing has ever replayed them, so every change to
detection or to the rails has been argued from examples rather than measured. This
script is the yardstick: it re-asks the questions and compares the answers to what
was decided.

Two things about the label set decide the whole design, and both are measurements
rather than opinions.

**A merge deletes a row, so a positive is not a pair.** All that survives a fold is
`project_alias.from_dedup_key` — the folded identity as a string — and the survivor.
The folded row's name, citations and tranches are gone, so the name and tranche
passes cannot be replayed against it and no amount of care recovers them. What *can*
be replayed is exactly the question the write path asks: an article arrives carrying
that identity, and does `upsert._find_duplicate_candidate` route it to the survivor
or mint a second row? That is the ingest gate, measured on 37 real folds, and it is
the number Census containment is supposed to move.

**That measures the gate's KEY path, and only that.** A real arriving record also
carries a project *name*, and the gate's second branch uses it — `Stargate Abilene`
arriving against a stored `Stargate Abilene` matches on a shared name token whatever
the companies are. A folded identity has no name left, so this cannot replay that
branch and does not pretend to: the score below is what the keys alone achieve. That
is the right yardstick for a change to the keys, and an understatement of the gate.

**Who wrote a label decides what it is worth.** On the database this was written
against, all 37 aliases were written by `tracker merge` — a person typing two ids,
after the review in `docs/merge-review-2026-08-05.md` — and all 7 parked pairs were
written by a model during an unattended run. So the positives are gold and the
negatives are a model's own past answers: replaying a model against them measures
agreement, not accuracy. Every count here is therefore reported per `decided_by`,
and a run that quotes the totals without that split is quoting the wrong number.

Nothing here writes. The database is opened read-only and the arriving records are
built in memory.

Usage:

    python scripts/eval_pairs.py --detection --db data/tracker.db
    python scripts/eval_pairs.py --detection --json > after.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tracker.capex import suspected_duplicates
from tracker.db import open_db, session_scope
from tracker.dedup import (
    all_keys,
    dedup_key,
    is_cross_granularity_match,
    shared_parties_across_companies,
)
from tracker.dupresolve import evidence_blocks_merge
from tracker.models import NotDuplicate, Project, ProjectAlias
from tracker.pairs import canonical
from tracker.upsert import _find_duplicate_candidate


@dataclass
class Label:
    """One recorded answer, and who recorded it."""

    kind: str  # "same" (an alias) | "different" (a parked pair)
    decided_by: str
    #: positives: the folded identity and the row it belongs to.
    folded_key: str = ""
    survivor_id: int = 0
    #: negatives: the two rows.
    a_id: int = 0
    b_id: int = 0
    reason: str = ""

    @property
    def gold(self) -> bool:
        """A person decided it. `decided_by` is the only thing that says so."""
        return self.decided_by == "operator"


@dataclass
class Outcome:
    """What replaying one label produced."""

    label: Label
    verdict: str
    detail: str = ""
    signals: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "kind": self.label.kind,
            "decided_by": self.label.decided_by,
            "gold": self.label.gold,
            "verdict": self.verdict,
            "detail": self.detail,
            "signals": self.signals,
        }
        if self.label.kind == "same":
            out |= {"folded_key": self.label.folded_key, "survivor_id": self.label.survivor_id}
        else:
            out |= {"a_id": self.label.a_id, "b_id": self.label.b_id}
        return out


def labels(session) -> list[Label]:
    """Every recorded answer in the database, positives first."""
    out = [
        Label(
            kind="same",
            decided_by=row.decided_by,
            folded_key=row.from_dedup_key,
            survivor_id=row.to_project_id,
        )
        for row in session.scalars(select(ProjectAlias)).all()
    ]
    out += [
        Label(
            kind="different",
            decided_by=row.decided_by,
            a_id=row.a_id,
            b_id=row.b_id,
            reason=row.reason or "",
        )
        for row in session.scalars(select(NotDuplicate)).all()
    ]
    return out


def _arriving(folded_key: str) -> dict[str, str | None] | None:
    """The payload an article carrying a folded identity would arrive with.

    A dedup key is `company|kind:locality|STATE`, all three parts already
    normalized. Feeding them back as the display values is safe because the
    normalizers are idempotent — and rather than assume that, the caller checks
    that the key round-trips and drops the label if it does not, so a
    normalization surprise is reported as an unscorable label instead of a
    silently wrong one.
    """
    try:
        company, locality, state = folded_key.split("|", 2)
        kind, name = locality.split(":", 1)
    except ValueError:
        return None
    if not company or not name:
        return None
    return {
        "company": company,
        "city": name if kind == "city" else None,
        "county": name if kind == "county" else None,
        "state": state,
        "name": None,
    }


def _signals(payload: dict[str, str | None], row: Project) -> list[str]:
    """Which key-level signals hold between an arriving identity and a stored row.

    Reported so a change in the gate's recall can be attributed. Computed from the
    same primitives the gate uses, not from its control flow — this says what is
    true of the pair, not which branch happened to fire.
    """
    incoming = all_keys(payload["company"], payload["city"], payload["county"], payload["state"])
    existing = all_keys(row.company, row.city, row.county, row.state)
    got: list[str] = []
    if incoming & existing:
        got.append("shared key")
    if any(is_cross_granularity_match(one, other) for one in incoming for other in existing):
        got.append("cross granularity")
    if shared_parties_across_companies(payload["company"], row.company):
        got.append("shared party")
    return got


def replay_positive(session, label: Label, twins: set[tuple[int, int]]) -> Outcome:
    """Would the write path route this folded identity back to its survivor?

    The alias itself is deliberately not consulted. `upsert_record` reads it after
    an exact-key miss, so in production the fold is remembered — but the question
    worth measuring is whether the duplicate would have been *prevented*, which is
    what `_find_duplicate_candidate` answers before any alias exists.
    """
    payload = _arriving(label.folded_key)
    if payload is None:
        return Outcome(label, "unscorable", "the key is not in company|kind:locality|STATE form")
    key = dedup_key(payload["company"], payload["city"], payload["county"], payload["state"])
    if key != label.folded_key:
        return Outcome(label, "unscorable", f"the key does not round-trip: {key!r}")

    survivor = session.get(Project, label.survivor_id)
    if survivor is None:
        return Outcome(label, "unscorable", f"survivor #{label.survivor_id} is gone")

    found = _find_duplicate_candidate(session, key, payload)
    signals = _signals(payload, survivor)
    if found is None:
        return Outcome(label, "missed", "the gate would have created a second row", signals)
    if found.id == label.survivor_id:
        return Outcome(label, "reached", f"routed to #{found.id}", signals)
    if canonical(found.id, label.survivor_id) in twins:
        # Routed to a row that is itself a suspected duplicate of the survivor —
        # `Cipher Stingray LLC` against `Cipher Digital Inc.`, both holding tranche
        # `stingray`. Scoring that as a miss would mark the gate wrong for a fold
        # nobody has performed yet. It found the campus; the campus is stored twice.
        return Outcome(
            label,
            "reached a twin",
            f"routed to #{found.id}, a suspected duplicate of #{label.survivor_id}",
            signals,
        )
    return Outcome(
        label,
        "wrong row",
        f"routed to #{found.id} ({found.company} — {found.name}), not #{label.survivor_id}",
        signals,
    )


def replay_negative(session, label: Label, raised: dict[tuple[int, int], object]) -> Outcome:
    """Is a pair somebody ruled out still raised, and would the rails now fold it?

    A parked pair is invisible to `suspected_duplicates` by default — that is what
    parking is for — so this asks with `include_parked=True`. Being raised is not a
    fault: the pair looked like a duplicate, which is why it was asked about. Being
    *mergeable* is the finding, because a rail that would fold a pair a person ruled
    out is a rail that is wrong.
    """
    pair = raised.get(canonical(label.a_id, label.b_id))
    if pair is None:
        return Outcome(label, "not raised", "detection no longer proposes this pair")
    a = session.get(Project, label.a_id)
    b = session.get(Project, label.b_id)
    if a is None or b is None:
        return Outcome(label, "unscorable", "one of the rows is gone")
    kinds = list(pair.kinds)
    blocked = evidence_blocks_merge(pair, a, b)
    reading = evidence_blocks_merge(pair, a, b, judge_read_the_sources=True)
    if blocked is None or reading is None:
        which = "both judges" if blocked is None and reading is None else "the reading judge"
        # Not "would be folded". No rail *refuses* it, so the whole decision rests
        # on the judge answering `different` — which makes these pairs the exact
        # population a change to the pair prompt has to be measured against.
        return Outcome(
            label, "no rail refuses", f"only the judge stands between {which} and a fold", kinds
        )
    return Outcome(label, "a rail refuses", blocked, kinds)


def detection(session) -> list[Outcome]:
    """Replay every label. Free: no model, no network."""
    raised = {
        canonical(p.a_id, p.b_id): p for p in suspected_duplicates(session, include_parked=True)
    }
    twins = set(raised)
    out = []
    for label in labels(session):
        if label.kind == "same":
            out.append(replay_positive(session, label, twins))
        else:
            out.append(replay_negative(session, label, raised))
    return out


def report(outcomes: list[Outcome]) -> None:
    """Print the scores, split by who decided — the split is the point."""
    for kind, title, good in (
        ("same", "POSITIVES — identities a merge folded away", "reached"),
        ("different", "NEGATIVES — pairs somebody ruled out", "a rail refuses"),
    ):
        rows = [o for o in outcomes if o.label.kind == kind]
        if not rows:
            continue
        print(f"\n{title}: {len(rows)}")
        for stratum, label in (
            ("operator", "decided by a person (gold)"),
            ("model", "decided by a model"),
        ):
            got = [o for o in rows if (o.label.gold if stratum == "operator" else not o.label.gold)]
            if not got:
                continue
            counts = collections.Counter(o.verdict for o in got)
            hit = counts.get(good, 0)
            print(f"  {label}: {len(got)}")
            for verdict, n in counts.most_common():
                mark = "ok " if verdict == good else "   "
                print(f"    {mark}{verdict:<18} {n:>3}")
            if kind == "same":
                near = sum(1 for o in got if o.verdict == "reached a twin")
                print(
                    f"    the key path finds the campus: {hit + near}/{len(got)}"
                    f"  ({hit} the survivor itself, {near} a row suspected of being it)"
                )
                print("    an arriving record also carries a name; this cannot replay that")

    signals = collections.Counter(
        "+".join(o.signals) or "none"
        for o in outcomes
        if o.label.kind == "same"
        and o.verdict in {"reached", "reached a twin", "missed", "wrong row"}
    )
    if signals:
        print("\nkey signals between the folded identity and its survivor")
        for combo, n in signals.most_common():
            print(f"  {combo:<32} {n:>3}")

    missed = [o for o in outcomes if o.verdict == "missed"]
    if missed:
        print(f"\nthe {len(missed)} the gate would not have caught")
        for o in missed:
            print(f"  {o.label.folded_key:<44} -> #{o.label.survivor_id}")

    bad = [o for o in outcomes if o.verdict in {"no rail refuses", "wrong row"}]
    if bad:
        print(f"\nwhere the rules and the recorded answer are not aligned: {len(bad)}")
        for o in bad:
            who = o.label.folded_key or f"#{o.label.a_id}+#{o.label.b_id}"
            print(f"  [{o.verdict}] {who} — {o.detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/tracker.db", help="database file")
    ap.add_argument("--detection", action="store_true", help="replay the labels. Free.")
    ap.add_argument("--json", action="store_true", help="one JSON object per label, for diffing")
    args = ap.parse_args()

    if not args.detection:
        ap.error("nothing to do: pass --detection")

    path = Path(args.db)
    if not path.is_file():
        print(f"database not found: {path}", file=sys.stderr)
        return 2

    engine = open_db(path, readonly=True)
    with session_scope(engine, commit=False) as session:
        outcomes = detection(session)

    if args.json:
        for outcome in outcomes:
            print(json.dumps(outcome.as_json(), sort_keys=True))
        return 0

    gold = sum(1 for o in outcomes if o.label.gold)
    print(
        f"{len(outcomes)} label(s) — {gold} decided by a person, {len(outcomes) - gold} by a model"
    )
    report(outcomes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
