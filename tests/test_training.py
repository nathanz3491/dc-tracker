"""The training labels the tracker keeps about its own values, and the export that joins them.

Each test is one promise `tracker.labels` and `tracker.training` make: a paid pass's
effect is recorded as it lands (and nothing for a dry run), a finding's outcome is
read from what its value did next, a snapshot history records changes rather than
nights, and the export carries the answer key's verdict beside the value it judged.
"""

from __future__ import annotations

import sqlite3

from tracker import judged, labels, training
from tracker.models import AnswerKey, Project


def _project(session, **kw) -> Project:
    row = Project(
        name=kw.pop("name", "Campus"),
        company="Compass Datacenters",
        state="AZ",
        city="Mesa",
        phase=kw.pop("phase", "announced"),
        dedup_key=kw.pop("dedup_key", "training-1"),
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def _pass(session, pid, *, commit=True, fields=None):
    def apply(item, prepared, answer):
        session.get(Project, item).phase = answer
        return judged.Done(result=item)

    return judged.run(
        session,
        [pid],
        kind=None,
        subject=str,
        fingerprint=str,
        projects=lambda item: [item],
        prepare=lambda item: item,
        ask=lambda prepared, _db: "construction",
        apply=apply,
        commit=commit,
        label="test-pass",
        fields=fields,
    )


def test_a_paid_pass_records_what_it_changed_and_what_it_left_standing(session, db_path):
    pid = _project(session, mw_planned=300.0).id

    _pass(session, pid, fields=lambda item: [(item, "phase"), (item, "mw_planned")])

    lines = labels.read(db_path, labels.VERDICTS)
    by_field = {line["field"]: line for line in lines}
    assert by_field["phase"]["verdict"] == "changed"
    assert (by_field["phase"]["old"], by_field["phase"]["new"]) == ("announced", "construction")
    assert by_field["mw_planned"]["verdict"] == "kept"
    assert {line["by"] for line in lines} == {"test-pass"}
    assert set(by_field) == {"phase", "mw_planned"}, "fields it did not look at say nothing"


def test_a_dry_run_records_nothing(session, db_path):
    pid = _project(session).id

    _pass(session, pid, commit=False)

    assert labels.read(db_path, labels.VERDICTS) == []


def test_a_finding_is_confirmed_when_its_value_moves_and_dismissed_when_it_goes_away():
    def line(night, code, pid, value):
        return {
            "night": night,
            "check": "logic",
            "code": code,
            "project_id": pid,
            "field": "mw_planned",
            "value": value,
        }

    lines = [
        line("2026-11-01", "moved", 1, 300),
        line("2026-11-02", "moved", 1, 400),  # the next night the value had changed
        line("2026-11-01", "gone", 2, 500),  # not raised on 11-02, value unchanged
        line("2026-11-02", "other", 9, 1),
        line("2026-11-01", "still", 3, 700),
        line("2026-11-02", "still", 3, 700),
    ]
    held = {(1, "mw_planned"): 400, (2, "mw_planned"): 500, (3, "mw_planned"): 700}

    got = training.finding_outcomes(lines, held)

    assert got[(1, "mw_planned")][0]["outcome"] == "confirmed"
    assert got[(2, "mw_planned")][0]["outcome"] == "dismissed"
    assert got[(3, "mw_planned")][0]["outcome"] == "open"


def test_the_history_records_each_change_not_each_night(tmp_path):
    for stamp, mw in (("20260901-180002", 300), ("20260902-180002", 300), ("20260903-180002", 450)):
        con = sqlite3.connect(tmp_path / f"tracker.backup-overnight-{stamp}.db")
        con.execute("CREATE TABLE project (id INTEGER PRIMARY KEY, phase TEXT, mw_planned REAL)")
        con.execute("INSERT INTO project VALUES (7, 'construction', ?)", (mw,))
        con.commit()
        con.close()

    got = training.history(tmp_path, {7})

    assert got[(7, "mw_planned")] == [
        {"night": "2026-09-01", "value": 300},
        {"night": "2026-09-03", "value": 450},
    ]
    assert got[(7, "phase")] == [{"night": "2026-09-01", "value": "construction"}]
    assert (7, "investment_usd") not in got, "a column the snapshot lacks is not invented"


def test_the_export_carries_the_answer_keys_verdict_on_the_held_value(session, db_path):
    pid = _project(session, mw_planned=300.0).id
    session.add(
        AnswerKey(
            project_id=pid, field="mw_planned", value="1000", status="verified", method="test"
        )
    )
    session.commit()

    entries = training.rows(session, [pid], db_path=db_path)

    assert [e["field"] for e in entries] == list(training.FIELDS)
    mw = next(e for e in entries if e["field"] == "mw_planned")
    assert mw["held"] == 300.0
    assert mw["answer"]["verdict"] == "wrong"
    assert mw["row"]["city"] == "Mesa"
    assert next(e for e in entries if e["field"] == "phase")["answer"] is None


def test_a_read_only_session_names_the_real_file(db_path, migrated_copy):
    """2026-11-01: the read-only engine's `file:` URI was taken for a path, and the
    night's findings went into a directory called `file:` inside the checkout."""
    from tracker.db import make_engine, session_scope

    migrated_copy(db_path)
    engine = make_engine(db_path, readonly=True)
    try:
        with session_scope(engine, commit=False) as s:
            assert labels.db_path_of(s).resolve() == db_path.resolve()
    finally:
        engine.dispose()
