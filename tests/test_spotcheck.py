"""The nightly spot check: a random sample of the night's changes, judged by a person.

The answer key says a night broke nothing already verified; it cannot say whether
what the night wrote is right. These pin what makes the sample a measure of that:
it is drawn at random from the night's changes to the fields a reader acts on, the
same night draws the same rows, a judged night is never redrawn, and the score counts
right against wrong with `unsure` apart.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracker import spotcheck
from tracker.changes import Changes, ValueChange
from tracker.cli import app
from tracker.db import init_db, session_scope
from tracker.models import Project, SpotCheck

runner = CliRunner()
NIGHT = dt.date(2026, 11, 15)


def _change(pid: int, field: str, before=None, after=1.0, **kw) -> ValueChange:
    return ValueChange(pid, f"Co — Campus {pid}", field, before, after, **kw)


def _campus(session, i: int = 0, **values) -> Project:
    project = Project(
        name=f"Campus {i}",
        company="Co",
        city="Abilene",
        state="TX",
        dedup_key=f"co|{i}|TX",
        **values,
    )
    session.add(project)
    session.flush()
    return project


def _changes(session, n: int, field: str = "mw_planned") -> Changes:
    """`n` changes, each on a campus of its own: a sample row points at its campus."""
    ids = [_campus(session, i).id for i in range(n)]
    return Changes(values=[_change(pid, field, before=None, after=float(pid)) for pid in ids])


def test_a_night_draws_at_most_its_size_from_the_fields_a_reader_acts_on(session):
    changes = _changes(session, 40)
    changes.values += [_change(i, "blocker", after="a summary") for i in range(100, 160)]
    changes.values += [_change(i, "county", after="Harris") for i in range(200, 230)]

    drawn = spotcheck.draw(session, changes, NIGHT)

    assert (drawn.candidates, drawn.drawn, drawn.held) == (40, spotcheck.SIZE, spotcheck.SIZE)
    held = spotcheck.listing(session, NIGHT)
    assert {c.field for c in held} == {"mw_planned"}, "blocker rewordings and Census counties"


def test_the_same_night_draws_the_same_rows_and_a_drawn_night_is_never_redrawn(
    session, tmp_path: Path
):
    changes = _changes(session, 40)
    spotcheck.draw(session, changes, NIGHT)
    first = {c.project_id for c in spotcheck.listing(session, NIGHT)}

    again = spotcheck.draw(session, changes, NIGHT)
    assert again.drawn == 0 and again.held == spotcheck.SIZE

    # A fresh database drawing the same night from the same changes picks the same rows,
    # so a re-run of the report cannot swap a judged sample for another.
    engine, _ = init_db(tmp_path / "other.db")
    with session_scope(engine) as other:
        spotcheck.draw(other, _changes(other, 40), NIGHT)
        assert {c.project_id for c in spotcheck.listing(other, NIGHT)} == first
    engine.dispose()


def test_a_small_night_draws_every_change_and_an_empty_one_draws_none(session):
    assert spotcheck.draw(session, _changes(session, 4), NIGHT).drawn == 4
    assert spotcheck.draw(session, Changes(), NIGHT + dt.timedelta(days=1)).drawn == 0


def test_the_question_follows_the_kind_of_change(session):
    one, two, three = (_campus(session, i).id for i in range(3))
    changes = Changes(
        values=[
            _change(one, "investment_usd", before=None, after=4e9),
            _change(two, "mw_planned", before=946, after=1200),
            _change(three, "investment_usd", before=6.8e9, after=None),
        ]
    )
    spotcheck.draw(session, changes, NIGHT)
    by_pid = {c.project_id: c for c in spotcheck.listing(session, NIGHT)}

    assert spotcheck.kind(by_pid[one]) == "set"
    assert spotcheck.question(by_pid[two]) == "Is 1,200 MW right as this campus's planned capacity?"
    assert spotcheck.kind(by_pid[three]) == "emptied"
    assert spotcheck.question(by_pid[three]).startswith("Was $6,800,000,000 wrong")


def test_what_stood_behind_the_value_is_kept_as_drawn(session):
    project = _campus(session, mw_planned=1)
    change = _change(
        project.id, "mw_planned", before=1, after=500, url="https://e.x/a", quote="500 MW"
    )
    spotcheck.draw(session, Changes(values=[change]), NIGHT)

    project.mw_planned = 600  # a later night moves the row
    session.flush()

    (check,) = spotcheck.listing(session, NIGHT)
    got = spotcheck.as_dict(check)
    assert (got["before"], got["after"], got["quote"], got["url"]) == (
        1,
        500,
        "500 MW",
        "https://e.x/a",
    )


def test_the_score_counts_right_against_wrong_and_keeps_unsure_apart(session):
    spotcheck.draw(session, _changes(session, 6), NIGHT)
    ids = [c.id for c in spotcheck.listing(session, NIGHT)]
    for check_id, verdict in zip(ids, ["right", "right", "right", "wrong", "unsure"], strict=False):
        spotcheck.mark(session, check_id, verdict, by="tester")

    got = spotcheck.score(session).total
    assert (got.right, got.wrong, got.unsure, got.open) == (3, 1, 1, 1)
    assert got.share == pytest.approx(0.75)
    low, high = got.interval
    assert 0.0 < low < 0.75 < high <= 1.0
    assert "3 of 4 right" in got.line() and "1 unsure" in got.line()


def test_a_second_answer_replaces_the_first_and_a_bad_one_is_refused(session):
    spotcheck.draw(session, _changes(session, 1), NIGHT)
    (check,) = spotcheck.listing(session, NIGHT)
    spotcheck.mark(session, check.id, "wrong", by="a", note="  a campus total  ")
    spotcheck.mark(session, check.id, "right", by="b")

    assert (check.verdict, check.checked_by, check.note) == ("right", "b", None)
    with pytest.raises(spotcheck.CheckError):
        spotcheck.mark(session, check.id, "maybe", by="a")
    with pytest.raises(spotcheck.CheckError):
        spotcheck.mark(session, 999_999, "right", by="a")


def test_the_night_is_read_from_the_loop_snapshot_name():
    name = Path("tracker.backup-overnight-20261115-180123.db")
    assert spotcheck.night_of(name) == NIGHT
    assert spotcheck.night_of(Path("before.db")) is None


def test_draw_show_mark_and_score_from_the_command_line(tmp_path: Path):
    path = tmp_path / "tracker.db"
    engine, _ = init_db(path)
    with session_scope(engine) as s:
        for i in range(3):
            _campus(s, i)
    engine.dispose()
    snapshot = tmp_path / "tracker.backup-overnight-20261115-180123.db"
    with sqlite3.connect(path) as con:
        con.execute("VACUUM INTO ?", (str(snapshot),))
    engine, _ = init_db(path)
    with session_scope(engine) as s:
        for i, project in enumerate(s.query(Project).order_by(Project.id)):
            project.mw_planned = 100.0 + i
    engine.dispose()

    drawn = runner.invoke(app, ["--db", str(path), "checks", "draw", "--against", str(snapshot)])
    assert drawn.exit_code == 0, drawn.output
    assert "3 of 3 changed value(s) drawn" in drawn.output

    shown = runner.invoke(app, ["--db", str(path), "--json", "checks", "show"])
    rows = json.loads(shown.output)
    assert [r["night"] for r in rows] == ["2026-11-15"] * 3

    # Marked through the library: a second writing command in this process would trip
    # the single-writer lock, which `atexit` only releases when the process ends.
    engine, _ = init_db(path)
    with session_scope(engine) as s:
        assert s.query(SpotCheck).count() == 3
        spotcheck.mark(s, rows[0]["id"], "wrong", by="tester")
    engine.dispose()

    scored = runner.invoke(app, ["--db", str(path), "--json", "checks"])
    total = json.loads(scored.output)["total"]
    assert (total["wrong"], total["open"]) == (1, 2)


def test_show_names_the_route_beside_a_borrowed_sentence(tmp_path: Path):
    """Flexential's 15.25 MW was two tranches' sum shown with one tranche's 2.25 MW
    sentence, which read as a sentence stating another figure."""
    path = tmp_path / "tracker.db"
    engine, _ = init_db(path)
    with session_scope(engine) as s:
        project = _campus(s)
        route = "the sum of 2 tranches: 'first phase' 2.25 MW, 'Plano' 13 MW"
        change = _change(project.id, "mw_planned", after=15.25, quote="adds 2.25 MW", via=route)
        spotcheck.draw(s, Changes(values=[change]), NIGHT)
    engine.dispose()

    shown = runner.invoke(app, ["--db", str(path), "checks", "show"], env={"COLUMNS": "200"})
    assert shown.exit_code == 0, shown.output
    assert "reached through the sum of 2 tranches" in shown.output


# --- the console's Admin page ---------------------------------------------------------


def test_an_admin_answers_from_the_console_and_a_reader_cannot(tmp_path, migrated_copy):
    import threading
    from http.server import ThreadingHTTPServer

    from tests.test_user_admin import _make, call, sign_in
    from tracker.db import open_db
    from tracker.webui.server import Console, Handler

    path = migrated_copy(tmp_path / "t.db")
    _make(path, "boss@example.com", admin=True, name="Boss")
    _make(path, "reader@example.com")
    with session_scope(open_db(path, readonly=False)) as session:
        spotcheck.draw(session, _changes(session, 3), NIGHT)
        check_id = spotcheck.listing(session, NIGHT)[0].id

    console = Console(path)
    handler = type("Bound", (Handler,), {"console": console})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    try:
        address = httpd.server_address
        _, _, reader = sign_in(address, "reader@example.com")
        status, _, _ = call(address, "/api/admin/checks", cookie=reader)
        assert status == 403
        body = {"id": check_id, "verdict": "wrong"}
        status, _, _ = call(address, "/api/admin/checks/mark", "POST", body, cookie=reader)
        assert status == 403

        _, _, boss = sign_in(address, "boss@example.com")
        status, page, _ = call(address, "/api/admin/checks", cookie=boss)
        assert status == 200
        assert page["night"] == "2026-11-15" and len(page["checks"]) == 3
        assert page["checks"][0]["question"].startswith("Is ")

        status, bad, _ = call(
            address, "/api/admin/checks/mark", "POST", {**body, "verdict": "maybe"}, cookie=boss
        )
        assert status == 400 and "verdict" in bad["error"]
        status, done, _ = call(
            address,
            "/api/admin/checks/mark",
            "POST",
            {**body, "note": "a county total"},
            cookie=boss,
        )
        assert status == 200 and done["check"]["checked_by"] == "Boss"

        status, page, _ = call(address, "/api/admin/checks?night=2026-11-15", cookie=boss)
        assert (page["ever"]["wrong"], page["ever"]["open"]) == (1, 2)
    finally:
        httpd.shutdown()
        httpd.server_close()
        console.close()
