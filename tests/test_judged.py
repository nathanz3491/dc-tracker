"""`judged.run`: the loop every paid pass shares, and the promises it makes.

Each test is one of the failures the shared runner was written to end: a batch that
lost every answer to a crash, a batch one bad item ended, a failed call asked again
the same night, a pass that ran past the money ceiling, and two questions about one
campus asked at once.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select

from tracker import declines, judged
from tracker.config import get_settings
from tracker.db import session_scope
from tracker.models import ModelDecline, Project


def _projects(session, n: int) -> list[Project]:
    rows = [
        Project(
            name=f"Campus {i}",
            company="Compass Datacenters",
            state="AZ",
            phase="construction",
            city="Mesa",
            dedup_key=f"judged-{i}",
        )
        for i in range(n)
    ]
    session.add_all(rows)
    session.commit()
    return rows


def _pass(session, items, *, ask=None, apply=None, **kw):
    """A pass that renames each project to what the model "said"."""

    def default_apply(item, prepared, answer):
        session.get(Project, item).name = answer
        return judged.Done(result=(item, answer))

    return judged.run(
        session,
        items,
        kind=kw.pop("kind", "risk"),
        subject=str,
        fingerprint=lambda item: f"print-{item}",
        projects=kw.pop("projects", lambda item: [item]),
        prepare=lambda item: item,
        ask=ask or (lambda prepared, _db: f"answered {prepared}"),
        apply=apply or default_apply,
        failed=lambda item, error: (item, f"failed: {error}"),
        **kw,
    )


def _names(engine) -> dict[int, str]:
    with session_scope(engine, commit=False) as other:
        return {p.id: p.name for p in other.scalars(select(Project))}


def test_a_crash_mid_batch_keeps_every_item_before_it(engine, session):
    """The night `risks confirm` was killed after its 39th call and kept nothing."""
    ids = [p.id for p in _projects(session, 4)]

    def apply(item, prepared, answer):
        if item == ids[2]:
            raise KeyboardInterrupt  # a kill, not an error: nothing may swallow it
        session.get(Project, item).name = answer
        return judged.Done(result=item)

    with pytest.raises(KeyboardInterrupt):
        _pass(session, ids, apply=apply)
    session.rollback()
    names = _names(engine)
    assert [names[i] for i in ids[:2]] == [f"answered {i}" for i in ids[:2]]
    assert names[ids[3]] == "Campus 3", "nothing after the crash ran"


def test_one_item_raising_is_rolled_back_and_the_rest_go_on(engine, session):
    ids = [p.id for p in _projects(session, 3)]

    def apply(item, prepared, answer):
        session.get(Project, item).name = answer
        if item == ids[1]:
            raise RuntimeError("IntegrityError on phase")
        return judged.Done(result=(item, answer))

    got = _pass(session, ids, apply=apply)
    names = _names(engine)
    assert names[ids[0]] == f"answered {ids[0]}" and names[ids[2]] == f"answered {ids[2]}"
    assert names[ids[1]] == "Campus 1", "the failed item's write was rolled back"
    assert got.failed == 1
    assert got.results[1] == (ids[1], "failed: IntegrityError on phase")


def test_a_failed_call_is_held_for_the_night_as_an_error(session):
    """A failed call is not a decision: held tonight, asked again tomorrow."""
    ids = [p.id for p in _projects(session, 2)]

    def ask(prepared, _db):
        if prepared == ids[0]:
            raise TimeoutError("provider timed out")
        return "fine"

    got = _pass(session, ids, ask=ask)
    assert got.failed == 1
    row = session.scalar(select(ModelDecline).where(ModelDecline.subject == str(ids[0])))
    assert row is not None and row.outcome == declines.ERROR
    assert "provider timed out" in row.reason
    again = _pass(session, ids)
    assert again.held == 1, "the failed one is held tonight"
    assert [r[0] for r in again.results] == [ids[1]]


def test_a_decline_is_recorded_on_the_evidence_shown_and_forgotten_when_decided(session):
    ids = [p.id for p in _projects(session, 1)]
    _pass(session, ids, apply=lambda i, p, a: judged.Done(result=i, decline="unclear", reason="r"))
    row = session.scalar(select(ModelDecline))
    assert (row.outcome, row.fingerprint, row.reason) == ("unclear", f"print-{ids[0]}", "r")
    _pass(session, ids, again=True, apply=lambda i, p, a: judged.Done(result=i, forget=True))
    assert session.scalar(select(ModelDecline)) is None


def test_the_ceiling_stops_before_the_next_item_and_keeps_what_was_done(
    session, tmp_path, monkeypatch
):
    ids = [p.id for p in _projects(session, 4)]
    ledger = tmp_path / "night.spend"
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setenv("TRACKER_SPEND_LEDGER", str(ledger))
    monkeypatch.setenv("TRACKER_SPEND_CEILING_CNY", "1")
    get_settings.cache_clear()

    def ask(prepared, _db):
        if prepared == ids[1]:
            # The second call is the one that crosses the line.
            ledger.write_text(
                "2026-09-26T12:00:00Z\t1\trisks\tdeepseek-flash\t0\t9000000\t0\t0\n",
                encoding="utf-8",
            )
        return "ok"

    got = _pass(session, ids, ask=ask)
    assert [r[0] for r in got.results] == ids[:2]
    assert got.ceiling and got.unreached == ids[2:]


def test_the_ceiling_line_is_printed_and_the_command_exits_zero(capsys):
    from tracker.cli._shared import _print_ceiling

    _print_ceiling(judged.Run(ceiling="¥9.00 spent against a ¥8.00 ceiling", unreached=[1, 2]))
    out = capsys.readouterr().out
    assert "spend ceiling reached" in out and "2" in out


def test_a_wave_never_holds_two_items_of_one_project():
    pairs = [(1, 2), (2, 3), (4, 5), (1, 6), (7, 8)]
    wave, rest = judged.waves(pairs, lambda pair: pair, cap=6)
    assert wave == [(1, 2), (4, 5), (7, 8)]
    assert rest == [(2, 3), (1, 6)]
    seen = [p for pair in wave for p in pair]
    assert len(seen) == len(set(seen))


def test_a_deferred_item_blocks_its_projects_so_order_per_project_is_kept():
    """(2, 3) waits for (1, 2); (3, 9) must then wait for (2, 3), though 3 is free."""
    wave, rest = judged.waves([(1, 2), (2, 3), (3, 9)], lambda pair: pair, cap=6)
    assert wave == [(1, 2)] and rest == [(2, 3), (3, 9)]


def test_a_wave_is_no_larger_than_the_workers():
    wave, rest = judged.waves([1, 2, 3, 4, 5], lambda item: [item], cap=2)
    assert wave == [1, 2] and rest == [3, 4, 5]


def test_answers_are_applied_in_input_order_whatever_order_they_arrive(session, monkeypatch):
    ids = [p.id for p in _projects(session, 4)]
    delay = {ids[0]: 0.15, ids[1]: 0.10, ids[2]: 0.05, ids[3]: 0.0}
    applied: list[int] = []
    threads: set[str] = set()

    def ask(prepared, _db):
        threads.add(threading.current_thread().name)
        time.sleep(delay[prepared])
        return "ok"

    def apply(item, prepared, answer):
        applied.append(item)
        return judged.Done(result=item)

    _pass(session, ids, ask=ask, apply=apply, workers=4)
    assert applied == ids
    assert len(threads) > 1, "the asks overlapped"


def test_one_worker_is_the_serial_loop(session):
    """prepare, ask, apply, commit — one item at a time, on the calling thread."""
    ids = [p.id for p in _projects(session, 3)]
    trail: list[str] = []
    main = threading.current_thread().name

    def prepare(item):
        trail.append(f"prepare {item}")
        return item

    def ask(prepared, db):
        assert threading.current_thread().name == main
        assert db is session, "with one worker the ask reads through the caller's session"
        trail.append(f"ask {prepared}")
        return "ok"

    def apply(item, prepared, answer):
        trail.append(f"apply {item}")
        return judged.Done(result=item)

    judged.run(
        session,
        ids,
        kind="risk",
        subject=str,
        fingerprint=str,
        projects=lambda item: [item],
        prepare=prepare,
        ask=ask,
        apply=apply,
        reads_db=True,
    )
    assert trail == [f"{step} {i}" for i in ids for step in ("prepare", "ask", "apply")]


def test_several_workers_read_through_sessions_of_their_own(session):
    ids = [p.id for p in _projects(session, 2)]
    seen: list[object] = []

    def ask(prepared, db):
        seen.append(db)
        return db.get(Project, prepared).name

    got = judged.run(
        session,
        ids,
        kind=None,
        subject=str,
        fingerprint=str,
        projects=lambda item: [item],
        prepare=lambda item: item,
        ask=ask,
        apply=lambda item, prepared, answer: judged.Done(result=answer),
        reads_db=True,
        workers=2,
    )
    assert got.results == ["Campus 0", "Campus 1"]
    assert all(db is not session for db in seen)


def test_held_back_items_do_not_use_up_the_limit(session):
    ids = [p.id for p in _projects(session, 4)]
    declines.record(session, "risk", str(ids[0]), f"print-{ids[0]}", outcome="unclear")
    session.commit()
    got = _pass(session, ids, limit=2)
    assert [r[0] for r in got.results] == ids[1:3]
    assert got.held == 1 and got.beyond == 1


def test_a_lazy_walk_takes_no_fingerprint_it_does_not_need(session):
    ids = [p.id for p in _projects(session, 5)]
    printed: list[int] = []

    def fingerprint(item):
        printed.append(item)
        return f"print-{item}"

    judged.run(
        session,
        ids,
        kind="risk",
        subject=str,
        fingerprint=fingerprint,
        projects=lambda item: [item],
        prepare=lambda item: item,
        ask=lambda prepared, _db: "ok",
        apply=lambda item, prepared, answer: judged.Done(result=item),
        limit=2,
        lazy=True,
    )
    assert printed == ids[:2], "only the two asked were fingerprinted, once each, to record"


def test_a_dry_run_commits_and_remembers_nothing(engine, session):
    ids = [p.id for p in _projects(session, 1)]
    _pass(
        session,
        ids,
        commit=False,
        apply=lambda i, p, a: judged.Done(result=i, decline="unclear"),
    )
    session.rollback()
    assert session.scalar(select(ModelDecline)) is None


def test_room_bounds_how_many_start(session):
    """A token budget: room for one more row, then none."""
    ids = [p.id for p in _projects(session, 3)]
    left = iter([1, 0])
    got = _pass(session, ids, workers=3, room=lambda: next(left))
    assert [r[0] for r in got.results] == ids[:1]
    assert got.stopped and got.unreached == ids[1:]


def test_waves_are_formed_after_the_last_wave_was_applied(session):
    """A duplicate pair whose row was merged away is asked about the row that holds
    it now — so which rows a pair touches is asked when its wave is formed, after
    the merges before it, not once up front."""
    a, b, c, d = (p.id for p in _projects(session, 4))
    folded: dict[int, int] = {}
    touched: list[tuple[tuple[int, int], list[int]]] = []

    def surviving(pair):
        rows = [folded.get(pair[0], pair[0]), folded.get(pair[1], pair[1])]
        touched.append((pair, rows))
        return rows

    def apply(pair, prepared, answer):
        if pair == (a, b):
            folded[b] = a  # b merged into a
        return judged.Done(result=pair)

    got = judged.run(
        session,
        [(a, b), (b, c), (d, c)],
        kind=None,
        subject=str,
        fingerprint=str,
        projects=surviving,
        prepare=lambda pair: pair,
        ask=lambda prepared, _db: "ok",
        apply=apply,
        workers=3,
    )
    assert got.results == [(a, b), (b, c), (d, c)]
    assert ((b, c), [a, c]) in touched, "asked about the row b was merged into"
