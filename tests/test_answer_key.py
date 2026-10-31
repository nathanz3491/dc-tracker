"""The answer key: verified answers, and how a database scores against them.

Nothing else in the tracker measured whether a stored value is true, so no change to
how values are chosen could be weighed on net. On 2026-10-30 a researched draft for
the 50 largest campuses disagreed with 83 of 259 stored answers. These tests pin how
one value is judged (a figure within 5%, a date inside the answer's span, a stage
exactly), how a database is scored and two compared, and that a merge keeps a
campus's answers.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tracker import answer_key
from tracker.answer_key import Answer, judge, parse, span, wilson
from tracker.cli import app
from tracker.db import init_db, session_scope
from tracker.models import AnswerKey, Project

runner = CliRunner()


def _answer(field: str, value, **kw) -> Answer:
    return Answer(project_id=1, field=field, value=value, status="verified", method="test", **kw)


def _campus(session, **values) -> Project:
    project = Project(
        name=values.pop("name", "Campus"),
        company=values.pop("company", "Co"),
        city=values.pop("city", "Abilene"),
        state="TX",
        dedup_key=values.pop("dedup_key", "co|city:x|TX"),
        phase=values.pop("phase", "announced"),
        **values,
    )
    session.add(project)
    session.flush()
    return project


# --- judging one value ---------------------------------------------------------------


def test_a_figure_within_five_percent_is_right_and_a_programme_total_is_not():
    answer = _answer("mw_planned", "1800")
    assert judge(answer, 1800.0) == "right"
    assert judge(answer, 1750.0) == "right", "publishers round"
    assert judge(answer, 1200.0) == "wrong", "IT load against facility power"
    assert judge(answer, None) == "empty"


def test_a_range_answer_takes_any_figure_inside_it():
    answer = _answer("investment_usd", None, low=6e9, high=10e9)
    assert judge(answer, 6e9) == "right"
    assert judge(answer, 8.5e9) == "right"
    assert judge(answer, 25e9) == "wrong", "Hassayampa's $25B is not the Tonopah complex's"


def test_an_answer_with_no_value_says_the_field_should_be_empty():
    answer = _answer("expected_online", None)
    assert judge(answer, None) == "right"
    assert judge(answer, dt.date(2026, 3, 1)) == "wrong", "a halted campus's old date"


def test_a_stage_must_match_and_may_name_alternatives():
    assert judge(_answer("phase", "paused"), "paused") == "right"
    assert judge(_answer("phase", "paused"), "construction") == "wrong"
    assert judge(_answer("phase", "paused|cancelled"), "cancelled") == "right"


def test_a_date_inside_the_answers_span_is_right_and_a_coarser_one_imprecise():
    day = _answer("first_announced", "2025-02-25", precision="day")
    assert judge(day, dt.date(2025, 2, 25)) == "right"
    assert judge(day, dt.date(2025, 2, 1), "month") == "imprecise", "agrees, but less precisely"
    assert judge(day, dt.date(2025, 1, 1), "year") == "imprecise"
    assert judge(day, dt.date(2025, 9, 1)) == "wrong", "the Stargate month, not the first report"
    assert judge(day, dt.date(2025, 2, 1)) == "wrong", "a month stored as the 1st at day precision"

    half = _answer("expected_online", "2027-01-01", precision="half")
    assert judge(half, dt.date(2027, 5, 15)) == "right", "first capacity in H1 2027"
    assert judge(half, dt.date(2028, 1, 1), "year") == "wrong", "completion, not first capacity"


@pytest.mark.parametrize(
    ("day", "precision", "first", "last"),
    [
        (dt.date(2026, 8, 17), "month", dt.date(2026, 8, 1), dt.date(2026, 8, 31)),
        (dt.date(2026, 8, 17), "quarter", dt.date(2026, 7, 1), dt.date(2026, 9, 30)),
        (dt.date(2026, 8, 17), "half", dt.date(2026, 7, 1), dt.date(2026, 12, 31)),
        (dt.date(2024, 2, 17), "month", dt.date(2024, 2, 1), dt.date(2024, 2, 29)),
        (dt.date(2026, 8, 17), "year", dt.date(2026, 1, 1), dt.date(2026, 12, 31)),
        (dt.date(2026, 8, 17), None, dt.date(2026, 8, 17), dt.date(2026, 8, 17)),
    ],
)
def test_a_dates_span_at_each_precision(day, precision, first, last):
    assert span(day, precision) == (first, last)


def test_the_interval_is_wilsons_and_stays_inside_zero_and_one():
    assert wilson(0, 0) is None
    low, high = wilson(5, 10)
    assert round(low, 3) == 0.237 and round(high, 3) == 0.763
    low, high = wilson(10, 10)
    assert high == 1.0 and 0.6 < low < 0.75


# --- parsing and loading -------------------------------------------------------------


@pytest.mark.parametrize(
    ("record", "words"),
    [
        (
            {
                "project_id": 1,
                "field": "customer",
                "value": "x",
                "status": "verified",
                "method": "m",
            },
            "field",
        ),
        (
            {"project_id": 1, "field": "phase", "value": "x", "status": "maybe", "method": "m"},
            "status",
        ),
        ({"project_id": 1, "field": "phase", "value": "x", "status": "verified"}, "method"),
        (
            {
                "project_id": 1,
                "field": "mw_planned",
                "value": "a lot",
                "status": "draft",
                "method": "m",
            },
            "number",
        ),
        (
            {
                "project_id": 1,
                "field": "first_announced",
                "value": "May 2025",
                "status": "draft",
                "method": "m",
            },
            "ISO",
        ),
        (
            {
                "project_id": 1,
                "field": "investment_usd",
                "low": 9,
                "high": 6,
                "status": "draft",
                "method": "m",
            },
            "above",
        ),
        ({"field": "phase", "status": "draft", "method": "m"}, "project_id"),
    ],
)
def test_a_bad_answer_is_refused_with_what_is_wrong(record, words):
    with pytest.raises(ValueError, match=words):
        parse(record)


def test_load_keeps_a_held_answer_unless_told_to_replace(session):
    project = _campus(session)
    first = parse(
        {
            "project_id": project.id,
            "field": "phase",
            "value": "paused",
            "status": "verified",
            "method": "a",
        }
    )
    second = parse(
        {
            "project_id": project.id,
            "field": "phase",
            "value": "cancelled",
            "status": "verified",
            "method": "b",
        }
    )
    assert answer_key.load(session, [first]).added == 1
    assert answer_key.load(session, [second]).kept == 1
    assert session.query(AnswerKey).one().value == "paused"
    assert answer_key.load(session, [second], replace=True).replaced == 1
    assert session.query(AnswerKey).one().value == "cancelled"
    missing = parse(
        {"project_id": 99999, "field": "phase", "value": "x", "status": "draft", "method": "m"}
    )
    assert answer_key.load(session, [missing]).missing == [99999]


# --- scoring and comparing -----------------------------------------------------------


def test_only_verified_answers_are_scored_unless_drafts_are_asked_for(session):
    project = _campus(session, phase="construction", mw_planned=1200.0)
    answer_key.load(
        session,
        [
            parse(
                {
                    "project_id": project.id,
                    "field": "phase",
                    "value": "paused",
                    "status": "verified",
                    "method": "m",
                }
            ),
            parse(
                {
                    "project_id": project.id,
                    "field": "mw_planned",
                    "value": "1200",
                    "status": "draft",
                    "method": "m",
                }
            ),
            parse(
                {
                    "project_id": project.id,
                    "field": "investment_usd",
                    "value": "1e9",
                    "status": "unsettled",
                    "method": "m",
                }
            ),
        ],
    )
    found = answer_key.score(session)
    assert found.fields["phase"].outcomes["wrong"] == 1
    assert found.fields["mw_planned"].judged == 0
    assert found.total.judged == 1
    with_drafts = answer_key.score(session, statuses=("verified", "draft"))
    assert with_drafts.fields["mw_planned"].outcomes["right"] == 1
    assert with_drafts.fields["investment_usd"].judged == 0, "unsettled is never scored"


def test_compare_lists_every_answer_a_change_moved(session):
    project = _campus(session, phase="construction")
    answer_key.load(
        session,
        [
            parse(
                {
                    "project_id": project.id,
                    "field": "phase",
                    "value": "paused",
                    "status": "verified",
                    "method": "m",
                }
            )
        ],
    )
    before = {project.id: answer_key.held_values(project)}
    project.phase = "paused"
    session.flush()
    (moved,) = answer_key.compare(session, before)
    assert (moved.field, moved.was, moved.now) == ("phase", "wrong", "right")
    assert answer_key.score(session, rows=before).fields["phase"].outcomes["wrong"] == 1
    assert answer_key.score(session, rows={}).absent == [project.id], (
        "a campus not there is not wrong"
    )


# --- a merge keeps the campus's answers ----------------------------------------------


def test_a_merge_carries_answers_to_the_survivor_and_the_survivors_own_stand(session):
    from tracker.merge import merge_projects

    keep = _campus(session, name="Project Jupiter", dedup_key="borderplex|city:santa teresa|NM")
    dupe = _campus(session, name="Stargate Dona Ana", dedup_key="oracle|city:las cruces|NM")
    answer_key.load(
        session,
        [
            parse(
                {
                    "project_id": keep.id,
                    "field": "phase",
                    "value": "construction",
                    "status": "verified",
                    "method": "m",
                }
            ),
            parse(
                {
                    "project_id": dupe.id,
                    "field": "phase",
                    "value": "announced",
                    "status": "verified",
                    "method": "m",
                }
            ),
            parse(
                {
                    "project_id": dupe.id,
                    "field": "mw_planned",
                    "value": "2450",
                    "status": "verified",
                    "method": "m",
                }
            ),
        ],
    )
    merge_projects(session, keep.id, [dupe.id], by="test")
    held = {a.field: a.value for a in session.query(AnswerKey).filter_by(project_id=keep.id)}
    assert held == {"phase": "construction", "mw_planned": "2450"}
    assert session.query(AnswerKey).filter_by(project_id=dupe.id).count() == 0


# --- the commands --------------------------------------------------------------------


def test_load_then_score_from_the_command_line(tmp_path: Path):
    path = tmp_path / "tracker.db"
    engine, _ = init_db(path)
    with session_scope(engine) as s:
        project = _campus(s, phase="construction", mw_planned=2100.0)
        pid = project.id
    engine.dispose()
    batch = tmp_path / "answers.json"
    batch.write_text(
        json.dumps(
            [
                {
                    "project_id": pid,
                    "field": "phase",
                    "value": "construction",
                    "status": "verified",
                    "method": "m",
                },
                {
                    "project_id": pid,
                    "field": "mw_planned",
                    "value": "900",
                    "status": "verified",
                    "method": "m",
                },
            ]
        ),
        encoding="utf-8",
    )
    loaded = runner.invoke(app, ["--db", str(path), "answers", "load", str(batch)])
    assert loaded.exit_code == 0, loaded.output
    assert "2 added" in loaded.output
    scored = runner.invoke(app, ["--db", str(path), "--json", "answers"])
    assert scored.exit_code == 0, scored.output
    fields = json.loads(scored.output)["now"]["fields"]
    assert fields["phase"]["outcomes"] == {"right": 1}
    assert fields["mw_planned"]["outcomes"] == {"wrong": 1}, (
        "the whole Abilene tract, not Microsoft's 900 MW"
    )
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps([{"project_id": pid, "field": "phase", "status": "verified"}]), encoding="utf-8"
    )
    refused = runner.invoke(app, ["--db", str(path), "answers", "load", str(bad)])
    assert refused.exit_code != 0 and "method" in refused.output
