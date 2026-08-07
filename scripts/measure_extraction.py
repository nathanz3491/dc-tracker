#!/usr/bin/env python
"""Measure what the stored data actually rests on, and whether that is improving.

    python scripts/measure_extraction.py
    python scripts/measure_extraction.py --root /path/to/install
    python scripts/measure_extraction.py --mutants

Reads the database, writes nothing, spends nothing, needs no API key. Like
`measure_evidence_gate.py` it lives outside CI on purpose: the database belongs
to an *install*, not a checkout, and the number worth knowing is the one about
the corpus you actually have.

The two companion scripts answer different questions and neither answers this
one. `measure_evidence_gate.py` asks whether the quotes we stored are real —
98.7% exact substrings, which is a statement about the quotes that *exist*.
`tracker audit` asks whether a number could be true at all. Neither counts how
much of the database is standing on nothing, because that is invisible from
inside any single row: every one of the defects below looks perfectly ordinary
until you ask which sentence supports it and find there isn't one.

Four sections:

1. **Evidence census.** Every stored value in `quality.MEASURED_FIELDS`, bucketed
   by what stands behind it. The bucket that matters is
   `confirmed_without_quote`: a value the row presents as established, with no
   sentence recorded for it. It is distinct from 待确认, which is the same
   absence *declared* — the gate working, not failing.

2. **Vintage stratigraphy.** Which prompt version produced each source, and where
   the defects sit. A defect count concentrated in old vintages is a remediation
   job that no per-row check will ever surface; one spread evenly across the
   current vintage is a live bug in the gate.

3. **Recency inversions.** Fields where two claims tied on credibility and the
   tiebreak — `fetched_at`, i.e. when the crawler happened to visit — kept the
   one that was published *earlier*. Crawl order is arbitrary with respect to
   the truth, so every one of these is decided by an accident of scheduling.

4. **Claim-envelope axes.** Coverage and default collapse per axis, once
   `source.claim_meta` exists. Reported as absent before then, deliberately: the
   harness has to be running against a real baseline before the thing it measures
   is built, or "did this improve" has no answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import select  # noqa: E402

from tracker import quality  # noqa: E402
from tracker.db import open_db, session_scope  # noqa: E402

#: Set by `--root`. None means "ask the package where it is installed".
_ROOT: Path | None = None


def _install_root() -> Path:
    if _ROOT is not None:
        return _ROOT
    from tracker.config import install_root

    return install_root()


def _db_path() -> Path:
    return _install_root() / "data" / "tracker.db"


def _rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def report_census(session) -> quality.Census:
    _rule("1. Evidence census — what every stored value rests on")

    census = quality.evidence_census(session)
    if not census.total:
        print("no stored values in the measured fields; nothing to report")
        return census

    print(f"{census.total} stored values across {len(quality.MEASURED_FIELDS)} fields\n")
    labels = {
        # Spelled out rather than 待确认, unlike the CLI. These are padded to a
        # fixed width to align a numeric column, and CJK is double-width in some
        # console fonts and single in others — so the column lands in a different
        # place depending on the terminal. The CLI can use the real term because
        # Rich measures character width; this cannot.
        quality.QUOTE_BACKED: "quote-backed             ",
        quality.FLAGGED: "unconfirmed (gate said so)",
        quality.SILENT_DEFECT: "CONFIRMED, NO QUOTE      ",
        quality.DERIVED_VALUE: "derived / inferred       ",
        quality.DEFAULTED_VALUE: "schema default           ",
        quality.NO_SOURCE: "no source at all         ",
    }
    for bucket in quality.BUCKETS:
        count = census.buckets.get(bucket, 0)
        print(f"  {labels[bucket]} {count:5d}   {census.share(bucket):6.1%}")

    print("\nper field, share quote-backed:")
    for name in quality.MEASURED_FIELDS:
        row = census.by_field.get(name, {})
        total = sum(row.values())
        if not total:
            continue
        backed = row.get(quality.QUOTE_BACKED, 0)
        defects = row.get(quality.SILENT_DEFECT, 0)
        flag = f"   {defects} with no quote" if defects else ""
        print(f"  {name:18s} {backed:4d}/{total:4d}  {backed / total:6.1%}{flag}")

    if census.defects:
        print(
            f"\n{census.defects} value(s) are stored as established with no sentence "
            "behind them.\nThese are not the unconfirmed tier — nothing on the row says "
            "they are unsupported,\nso every reader and every rollup treats them as facts."
        )
    else:
        print("\nNo value is stored as established without a sentence behind it.")
    return census


def report_vintages(session, census: quality.Census) -> None:
    _rule("2. Vintage stratigraphy — which prompt version produced what")

    from sqlalchemy import func

    from tracker.models import Source

    rows = session.execute(select(Source.extractor, func.count()).group_by(Source.extractor)).all()
    counts: dict[str, int] = {}
    for extractor, count in rows:
        counts[quality.vintage(extractor)] = counts.get(quality.vintage(extractor), 0) + count

    try:
        from tracker.prompts import load_prompt

        current = load_prompt("extract-v1").stamp
    except Exception:  # pragma: no cover - a missing prompt file is not this script's problem
        current = "unknown"

    print(f"current extract prompt: {current}\n")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        defects = census.defects_by_vintage.get(name, 0)
        mark = "  <-- CURRENT" if name == current else ""
        note = f"   {defects} defect(s)" if defects else ""
        print(f"  {count:5d} sources  {name:32s}{note}{mark}")

    stale = sum(
        n
        for v, n in counts.items()
        if v != current and not v.startswith("derived") and not v.startswith("inferred")
    )
    if stale:
        print(
            f"\n{stale} extracted source(s) are not on the current prompt. Every gate "
            "improvement\nsince they were written has never been applied to them, and "
            "nothing re-reads\nthem: `source.extractor` is never compared against the "
            "current stamp."
        )
    if census.defects and census.defects_by_vintage:
        on_current = census.defects_by_vintage.get(current, 0)
        if on_current == 0:
            print(
                "\nEvery defect sits on a superseded prompt. The gate is working; this "
                "is a\nremediation job, not a bug."
            )
        else:
            print(
                f"\n*** {on_current} defect(s) come from the CURRENT prompt. That is a "
                "live gate\nfailure, not stratigraphy, and re-extraction will not fix "
                "it. ***"
            )


def report_inversions(session) -> None:
    _rule("3. Recency inversions — crawl order beating publication order")

    inversions = quality.recency_inversions(session)
    if not inversions:
        print("none: no tied claim was decided against publication order")
        return

    print(
        f"{len(inversions)} field value(s) kept an older article over a newer one "
        "of equal\ncredibility, because the tiebreak is `fetched_at` — when the "
        "crawler visited,\nnot when anybody published.\n"
    )
    for inv in inversions:
        print(f"  {inv.summary}")
    print(
        "\nThis is a floor, not a total. Only ties are counted; an old high-weight "
        "source\nbeating a new low-weight one is the same disease but is the "
        "weighting policy\nworking as designed, so it is deliberately not counted here."
    )


def report_axes(session) -> None:
    _rule("4. Claim-envelope axes — coverage and default collapse")

    stats = quality.axis_census(session)
    if not stats:
        print(
            "no `claim_meta` recorded yet — the envelope has not landed, or no source "
            "carries it.\nBaseline for the axes is therefore zero, which is the number "
            "the next run has to beat."
        )
        return

    for axis, stat in sorted(stats.items()):
        verdict = ""
        if stat.modal_share > quality.DEFAULT_COLLAPSE_CEILING:
            verdict = (
                f"   *** COLLAPSED: {stat.modal_share:.0%} on {stat.modal_value!r} — "
                "a default, not information ***"
            )
        print(
            f"  {axis:10s} coverage {stat.coverage:6.1%}  "
            f"modal {stat.modal_value!s:14s} {stat.modal_share:6.1%}{verdict}"
        )


def report_mutants(db: Path) -> None:
    """Plant known faults in a *copy* of the database and count what is caught.

    `HANDOFF.md` and `CHANGELOG.md` both cite "16 planted mutants, all caught" as
    the evidence for `tracker audit`. No script, test or commit contains it — the
    run was manual, against a copy of a live database nobody kept, so the claim
    has never been reproducible. This makes it so.

    Every mutation is applied to a temporary copy and thrown away. The live
    database is opened read-only and never written.
    """
    import sqlite3
    import tempfile

    from tracker import audit as audit_mod
    from tracker.models import Project, Source

    _rule("5. Planted mutants — does anything actually catch a wrong number?")

    # `ignore_cleanup_errors` because Windows will not unlink a file SQLite
    # still holds, and the pool is closed by garbage collection rather than by
    # `dispose()` alone. The measurement has already finished by then; failing
    # the run over a temp file the OS will reap anyway would be theatre.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "mutants.db"
        source = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(str(copy))
        source.backup(target)
        target.close()
        source.close()

        engine = open_db(copy, readonly=False)
        caught = planted = 0

        with session_scope(engine) as session:
            # A capacity three orders of magnitude too large, on rows that are
            # currently plausible. This is the project-72 shape: self-consistent,
            # uncontradicted, and wrong by a thousandfold.
            rows = session.scalars(
                select(Project).where(Project.mw_planned.is_not(None)).limit(8)
            ).all()
            for project in rows:
                before = {f.code for f in audit_mod.check_project(project)}
                project.mw_planned = (project.mw_planned or 1) * 1000
                planted += 1
                after = {f.code for f in audit_mod.check_project(project)}
                if after - before:
                    caught += 1
            session.rollback()

        with session_scope(engine) as session:
            # A quote removed from a source that has one: the exact shape of the
            # 89 legacy defects, and `quality.silent_defects` must see it appear.
            before = len(quality.silent_defects(session))
            rows = session.scalars(select(Source).where(Source.quotes.is_not(None)).limit(8)).all()
            stripped = 0
            for row in rows:
                row.quotes = None
                stripped += 1
            session.flush()
            after = len(quality.silent_defects(session))
            session.rollback()

        print(f"  capacity inflated 1000x on {planted} project(s): {caught} caught by `audit`")
        print(
            f"  quotes stripped from {stripped} source(s): silent defects "
            f"{before} -> {after} ({after - before} newly visible)"
        )
        if caught < planted:
            print(
                f"\n*** {planted - caught} inflated figure(s) went unflagged. "
                "That is a gap in `audit`, not a statistic. ***"
            )
        if after <= before:
            print(
                "\n*** Stripping quotes did not raise the defect count. The census "
                "is not measuring what it claims to. ***"
            )


def main(argv: list[str]) -> int:
    global _ROOT
    if "--root" in argv:
        _ROOT = Path(argv[argv.index("--root") + 1]).resolve()

    # This reports through a plain `print`, not Rich, so nothing is negotiating
    # the console encoding on our behalf: on a legacy Windows codepage every
    # em-dash in a section heading arrives as mojibake. `errors="replace"` rather
    # than a bare reconfigure, so a console that still cannot render a character
    # prints a placeholder instead of raising halfway through a report.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    db = _db_path()
    if not db.exists():
        sys.exit(f"no database at {db}")

    engine = open_db(db, readonly=True)
    with session_scope(engine, commit=False) as session:
        print(f"database: {db}")
        census = report_census(session)
        report_vintages(session, census)
        report_inversions(session)
        report_axes(session)

    if "--mutants" in argv:
        report_mutants(db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
